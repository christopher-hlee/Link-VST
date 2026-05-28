#pragma once
#include <vector>
#include <atomic>
#include <mutex>
#include <string>
#include <functional>

/**
 * PreviewPlayer — fetches WAV audio from the server preview endpoint and
 * plays it back through the plugin's stereo audio output.
 *
 * Usage:
 *   player.FetchAndPlay(libraryId, apiBaseUrl, apiKey, onError);
 *   player.Stop();
 *   // In ProcessBlock:
 *   player.Process(outputs, nFrames, sampleRate);
 */
class PreviewPlayer {
public:
    PreviewPlayer() = default;

    // Fetch audio for library_id from server, then start playback.
    // Non-blocking — fetches on a background thread.
    void FetchAndPlay(int library_id,
                      const std::string& base_url,
                      const std::string& api_key,
                      std::function<void(std::string)> on_error = nullptr);

    void Stop();
    bool IsPlaying() const { return mPlaying.load(); }

    // Call from ProcessBlock — renders into outputs[0] (L) and outputs[1] (R).
    // outputs must have at least 2 channels. nFrames is the block size.
    void Process(double** outputs, int nFrames, double sample_rate);

private:
    std::vector<float> mBuffer;     // interleaved stereo float32 PCM
    std::atomic<int>   mPlayPos{0};
    std::atomic<bool>  mPlaying{false};
    std::atomic<int>   mSampleRate{44100};
    mutable std::mutex mMutex;

    void LoadWav(const std::vector<uint8_t>& wav_bytes);
    static std::vector<uint8_t> FetchBytes(const std::string& url,
                                            const std::string& api_key);
};
