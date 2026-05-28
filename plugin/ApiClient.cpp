#include "ApiClient.h"
#include <curl/curl.h>
#include <nlohmann/json.hpp>
#include <thread>
#include <stdexcept>
#include <sstream>

using json = nlohmann::json;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static size_t CurlWriteCb(char* ptr, size_t size, size_t nmemb, std::string* out) {
    out->append(ptr, size * nmemb);
    return size * nmemb;
}

static const char kB64Chars[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::vector<uint8_t> ApiClient::Base64Decode(const std::string& in) {
    // Build reverse lookup
    uint8_t rev[256] = {};
    for (int i = 0; i < 64; ++i)
        rev[(uint8_t)kB64Chars[i]] = (uint8_t)i;

    std::vector<uint8_t> out;
    out.reserve(in.size() * 3 / 4);

    uint32_t buf = 0;
    int bits = 0;
    for (unsigned char c : in) {
        if (c == '=' || c == '\n' || c == '\r') continue;
        buf = (buf << 6) | rev[c];
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out.push_back((buf >> bits) & 0xFF);
        }
    }
    return out;
}

// ---------------------------------------------------------------------------
// Constructor
// ---------------------------------------------------------------------------

ApiClient::ApiClient(std::string base_url, std::string api_key)
    : mBaseUrl(std::move(base_url)), mApiKey(std::move(api_key))
{
    curl_global_init(CURL_GLOBAL_ALL);
}

// ---------------------------------------------------------------------------
// Core request (sync, called from threads or directly)
// ---------------------------------------------------------------------------

std::string ApiClient::DoRequest(const std::string& method,
                                  const std::string& path,
                                  const std::string& body,
                                  const std::string& content_type)
{
    CURL* curl = curl_easy_init();
    if (!curl) throw std::runtime_error("curl_easy_init failed");

    std::string url = mBaseUrl + path;
    std::string response;

    struct curl_slist* headers = nullptr;
    if (!mApiKey.empty())
        headers = curl_slist_append(headers, ("Authorization: Bearer " + mApiKey).c_str());
    if (!body.empty())
        headers = curl_slist_append(headers, ("Content-Type: " + content_type).c_str());
    headers = curl_slist_append(headers, "Accept: application/json");

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, CurlWriteCb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 60L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 0L);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);

    if (method == "POST") {
        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long)body.size());
    } else if (method == "DELETE") {
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "DELETE");
    }
    // GET is default

    CURLcode res = curl_easy_perform(curl);
    curl_slist_free_all(headers);

    long http_code = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
    curl_easy_cleanup(curl);

    if (res != CURLE_OK)
        throw std::runtime_error(std::string("network: ") + curl_easy_strerror(res));
    if (http_code >= 400)
        throw std::runtime_error("server error " + std::to_string(http_code) + ": " + response);

    return response;
}

// ---------------------------------------------------------------------------
// Upload MIDI (async — multipart/form-data)
// ---------------------------------------------------------------------------

void ApiClient::UploadMidi(const std::string& filename,
                            const std::vector<uint8_t>& midi_bytes,
                            UploadCallback callback)
{
    std::thread([this, filename, midi_bytes, callback]() {
        try {
            CURL* curl = curl_easy_init();
            if (!curl) throw std::runtime_error("curl_easy_init failed");

            std::string url = mBaseUrl + "/api/upload-midi";
            std::string response;

            struct curl_slist* headers = nullptr;
            if (!mApiKey.empty())
                headers = curl_slist_append(headers, ("Authorization: Bearer " + mApiKey).c_str());

            curl_mime* form = curl_mime_init(curl);
            curl_mimepart* field = curl_mime_addpart(form);
            curl_mime_name(field, "file");
            curl_mime_data(field, reinterpret_cast<const char*>(midi_bytes.data()),
                           midi_bytes.size());
            curl_mime_filename(field, filename.c_str());
            curl_mime_type(field, "audio/midi");

            curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
            curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
            curl_easy_setopt(curl, CURLOPT_MIMEPOST, form);
            curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, CurlWriteCb);
            curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
            curl_easy_setopt(curl, CURLOPT_TIMEOUT, 30L);
            curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 0L);

            CURLcode res = curl_easy_perform(curl);

            long http_code = 0;
            curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
            curl_mime_free(form);
            curl_slist_free_all(headers);
            curl_easy_cleanup(curl);

            if (res != CURLE_OK)
                throw std::runtime_error(curl_easy_strerror(res));
            if (http_code >= 400)
                throw std::runtime_error("HTTP " + std::to_string(http_code) + ": " + response);

            auto j = json::parse(response);
            callback(true, j.value("message", "Uploaded"), "");

        } catch (const std::exception& e) {
            callback(false, "", e.what());
        }
    }).detach();
}

// ---------------------------------------------------------------------------
// Generate (async — JSON POST)
// ---------------------------------------------------------------------------

void ApiClient::Generate(int count,
                          const std::string& phrase_type,
                          const std::string& key,
                          const std::string& mode,
                          int bars,
                          const std::string& hint,
                          bool variety,
                          GenerateCallback callback)
{
    std::thread([=, this]() {
        try {
            json req;
            req["count"]   = count;
            req["bars"]    = bars;
            req["variety"] = variety;
            if (!phrase_type.empty()) req["phrase_type"] = phrase_type;
            if (!key.empty())         req["key"]         = key;
            if (!mode.empty())        req["mode"]        = mode;
            if (!hint.empty())        req["hint"]        = hint;

            std::string body = req.dump();
            std::string resp = DoRequest("POST", "/api/generate", body, "application/json");

            auto j = json::parse(resp);
            std::vector<PhraseInfo> phrases;

            for (const auto& p : j.at("phrases")) {
                PhraseInfo info;
                info.phrase_type = p.value("phrase_type", "");
                info.key         = p.value("key", "C");
                info.mode        = p.value("mode", "major");
                info.tempo_bpm   = p.value("tempo_bpm", 120);
                info.bars        = p.value("bars", 4);
                info.description = p.value("description", "");
                info.midi_bytes  = Base64Decode(p.value("midi_b64", ""));
                phrases.push_back(std::move(info));
            }

            callback(true, std::move(phrases), "");

        } catch (const std::exception& e) {
            callback(false, {}, e.what());
        }
    }).detach();
}

// ---------------------------------------------------------------------------
// Library — synchronous (called from background RefreshLibrary thread)
// ---------------------------------------------------------------------------

std::vector<PhraseInfo> ApiClient::GetLibrary() {
    std::vector<PhraseInfo> out;
    try {
        std::string resp = DoRequest("GET", "/api/library", "", "");
        auto j = json::parse(resp);
        for (const auto& item : j.at("items")) {
            PhraseInfo info;
            info.phrase_type = item.value("phrase_type", "");
            info.key         = item.value("key", "C");
            info.mode        = item.value("mode", "major");
            info.tempo_bpm   = item.value("tempo_bpm", 120);
            info.bars        = item.value("bars", 4);
            info.description = item.value("description", "");
            // midi_bytes not included in list — fetched separately on demand
            out.push_back(std::move(info));
        }
    } catch (...) {}
    return out;
}

std::vector<uint8_t> ApiClient::GetMidi(int id) {
    try {
        std::string resp = DoRequest("GET", "/api/library/" + std::to_string(id) + "/midi", "", "");
        auto j = json::parse(resp);
        return Base64Decode(j.value("midi_b64", ""));
    } catch (...) {
        return {};
    }
}

bool ApiClient::DeleteLibraryItem(int id) {
    try {
        DoRequest("DELETE", "/api/library/" + std::to_string(id), "", "");
        return true;
    } catch (...) {
        return false;
    }
}
