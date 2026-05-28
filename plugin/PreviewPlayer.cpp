#include "PreviewPlayer.h"
#include <curl/curl.h>
#include <thread>
#include <cstring>
#include <cmath>
#include <stdexcept>

// ─── libcurl write callback ────────────────────────────────────────────────

static size_t WriteCb(char* ptr, size_t size, size_t nmemb, std::vector<uint8_t>* out) {
    out->insert(out->end(), ptr, ptr + size * nmemb);
    return size * nmemb;
}

// ─── Minimal WAV decoder (PCM 16-bit or 32-bit float, mono or stereo) ──────

struct WavHeader {
    char     riff[4];
    uint32_t chunk_size;
    char     wave[4];
    char     fmt[4];
    uint32_t fmt_size;
    uint16_t audio_format;  // 1=PCM, 3=float
    uint16_t num_channels;
    uint32_t sample_rate;
    uint32_t byte_rate;
    uint16_t block_align;
    uint16_t bits_per_sample;
};

void PreviewPlayer::LoadWav(const std::vector<uint8_t>& wav_bytes) {
    if (wav_bytes.size() < 44) return;

    const uint8_t* p = wav_bytes.data();
    WavHeader hdr;
    memcpy(&hdr, p, sizeof(WavHeader));

    if (strncmp(hdr.riff, "RIFF", 4) || strncmp(hdr.wave, "WAVE", 4)) return;

    // Find data chunk
    size_t offset = 12;
    uint32_t data_size = 0;
    while (offset + 8 <= wav_bytes.size()) {
        char chunk_id[5] = {};
        memcpy(chunk_id, p + offset, 4);
        uint32_t chunk_size;
        memcpy(&chunk_size, p + offset + 4, 4);
        if (strncmp(chunk_id, "data", 4) == 0) {
            offset += 8;
            data_size = chunk_size;
            break;
        }
        offset += 8 + chunk_size;
    }
    if (!data_size || offset + data_size > wav_bytes.size()) return;

    const uint8_t* samples = p + offset;
    int channels  = hdr.num_channels;
    int bits      = hdr.bits_per_sample;
    int fmt       = hdr.audio_format;
    int nSamples  = data_size / (bits / 8);
    int nFrames   = nSamples / channels;

    std::vector<float> pcm;
    pcm.reserve(nFrames * 2);  // always store as interleaved stereo

    auto readPCM = [&](int i) -> float {
        if (fmt == 3 && bits == 32) {
            float v; memcpy(&v, samples + i * 4, 4); return v;
        } else if (bits == 16) {
            int16_t v; memcpy(&v, samples + i * 2, 2);
            return v / 32768.0f;
        } else if (bits == 24) {
            int32_t v = 0;
            memcpy(reinterpret_cast<uint8_t*>(&v) + 1, samples + i * 3, 3);
            return (v >> 8) / 8388608.0f;
        }
        return 0.0f;
    };

    for (int f = 0; f < nFrames; ++f) {
        float l = readPCM(f * channels + 0);
        float r = (channels > 1) ? readPCM(f * channels + 1) : l;
        pcm.push_back(l);
        pcm.push_back(r);
    }

    {
        std::lock_guard<std::mutex> lock(mMutex);
        mBuffer = std::move(pcm);
        mPlayPos.store(0);
        mSampleRate.store((int)hdr.sample_rate);
    }
    mPlaying.store(true);
}

// ─── HTTP fetch ───────────────────────────────────────────────────────────

std::vector<uint8_t> PreviewPlayer::FetchBytes(const std::string& url,
                                                const std::string& api_key) {
    CURL* curl = curl_easy_init();
    if (!curl) throw std::runtime_error("curl init failed");

    std::vector<uint8_t> buf;
    struct curl_slist* headers = nullptr;
    if (!api_key.empty())
        headers = curl_slist_append(headers, ("Authorization: Bearer " + api_key).c_str());

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteCb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &buf);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 30L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 0L);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);

    CURLcode res = curl_easy_perform(curl);
    curl_slist_free_all(headers);

    long http_code = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
    curl_easy_cleanup(curl);

    if (res != CURLE_OK)
        throw std::runtime_error(curl_easy_strerror(res));
    if (http_code >= 400)
        throw std::runtime_error("HTTP " + std::to_string(http_code));

    return buf;
}

// ─── Public API ───────────────────────────────────────────────────────────

void PreviewPlayer::FetchAndPlay(int library_id,
                                  const std::string& base_url,
                                  const std::string& api_key,
                                  std::function<void(std::string)> on_error)
{
    Stop();  // stop any current playback

    std::string url = base_url + "/api/preview/" + std::to_string(library_id);

    std::thread([this, url, api_key, on_error]() {
        try {
            auto bytes = FetchBytes(url, api_key);
            LoadWav(bytes);
        } catch (const std::exception& e) {
            if (on_error) on_error(e.what());
        }
    }).detach();
}

void PreviewPlayer::Stop() {
    mPlaying.store(false);
    mPlayPos.store(0);
}

// ─── ProcessBlock ─────────────────────────────────────────────────────────

void PreviewPlayer::Process(double** outputs, int nFrames, double /*sample_rate*/) {
    if (!mPlaying.load()) {
        // Silence — clear outputs
        for (int ch = 0; ch < 2; ++ch)
            for (int i = 0; i < nFrames; ++i)
                outputs[ch][i] = 0.0;
        return;
    }

    std::lock_guard<std::mutex> lock(mMutex);
    int pos = mPlayPos.load();
    int bufStereoFrames = (int)mBuffer.size() / 2;

    for (int i = 0; i < nFrames; ++i) {
        if (pos >= bufStereoFrames) {
            // Reached end — silence remainder
            for (int j = i; j < nFrames; ++j) {
                outputs[0][j] = 0.0;
                outputs[1][j] = 0.0;
            }
            mPlaying.store(false);
            break;
        }
        outputs[0][i] = mBuffer[pos * 2 + 0];
        outputs[1][i] = mBuffer[pos * 2 + 1];
        ++pos;
    }
    mPlayPos.store(pos);
}
