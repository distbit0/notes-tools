import base64
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request


GOOGLE_CLOUD_TTS_BASE_URL = "https://texttospeech.googleapis.com/v1"


def get_google_cloud_access_token(text_to_speech_config):
    command = [
        text_to_speech_config["gcloudCommand"],
        "auth",
        "print-access-token",
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            f"Google Cloud CLI does not exist: {text_to_speech_config['gcloudCommand']}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Google Cloud authentication timed out") from error
    except subprocess.CalledProcessError as error:
        error_message = (error.stderr or "").strip()
        if error_message:
            raise RuntimeError(
                f"Google Cloud authentication failed: {error_message[:300]}"
            ) from error
        raise RuntimeError("Google Cloud authentication failed") from error

    access_token = result.stdout.strip()
    if not access_token:
        raise RuntimeError("Google Cloud authentication returned an empty access token")
    return access_token


def build_google_cloud_request(text_to_speech_config, url, method, request_body=None):
    headers = {
        "Accept": "application/json",
        "Authorization": (
            "Bearer " + get_google_cloud_access_token(text_to_speech_config)
        ),
        "x-goog-user-project": text_to_speech_config["quotaProject"],
    }
    data = None
    if request_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(request_body).encode("utf-8")
    return urllib.request.Request(url, data=data, headers=headers, method=method)


def read_google_cloud_json(request, operation):
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Google Cloud TTS {operation} failed with HTTP {error.code}: "
            f"{error_body[:300]}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Google Cloud TTS {operation} request failed: {error.reason}"
        ) from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RuntimeError(
            f"Google Cloud TTS {operation} returned invalid JSON"
        ) from error


def fetch_google_cloud_voice_names(text_to_speech_config):
    query = urllib.parse.urlencode(
        {"languageCode": text_to_speech_config["languageCode"]}
    )
    request = build_google_cloud_request(
        text_to_speech_config,
        f"{GOOGLE_CLOUD_TTS_BASE_URL}/voices?{query}",
        "GET",
    )
    payload = read_google_cloud_json(request, "voice listing")
    voices = payload.get("voices") if isinstance(payload, dict) else None
    if not isinstance(voices, list):
        raise RuntimeError("Google Cloud TTS voice listing omitted the voices list")

    voice_name_prefix = text_to_speech_config["voiceNamePrefix"]
    voice_names = sorted(
        {
            voice["name"]
            for voice in voices
            if isinstance(voice, dict)
            and isinstance(voice.get("name"), str)
            and voice["name"].startswith(voice_name_prefix)
        }
    )
    if not voice_names:
        raise RuntimeError(
            "Google Cloud TTS returned no voices matching configured prefix "
            f"{voice_name_prefix!r}"
        )
    return voice_names


def fetch_google_cloud_text_to_speech_audio(text_to_speech_config, habit_text):
    request_body = {
        "input": {"text": habit_text},
        "voice": {
            "languageCode": text_to_speech_config["languageCode"],
            "name": text_to_speech_config["voiceName"],
        },
        "audioConfig": {
            "audioEncoding": text_to_speech_config["audioEncoding"],
        },
    }
    request = build_google_cloud_request(
        text_to_speech_config,
        f"{GOOGLE_CLOUD_TTS_BASE_URL}/text:synthesize",
        "POST",
        request_body,
    )
    payload = read_google_cloud_json(request, "synthesis")
    encoded_audio = payload.get("audioContent") if isinstance(payload, dict) else None
    if not isinstance(encoded_audio, str) or not encoded_audio:
        raise RuntimeError("Google Cloud TTS synthesis omitted audioContent")
    try:
        audio_bytes = base64.b64decode(encoded_audio, validate=True)
    except (ValueError, TypeError) as error:
        raise RuntimeError(
            "Google Cloud TTS synthesis returned invalid base64 audio"
        ) from error
    if not audio_bytes:
        raise RuntimeError("Google Cloud TTS synthesis returned empty audio")
    return audio_bytes
