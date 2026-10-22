"""DashScope ASR, TTS, and Omni Realtime message builders. No network calls."""

import json
import uuid


def asr_run_task(model: str, *, language: str = "zh", task_id: str | None = None) -> dict:
    return {
        "header": {
            "action": "run-task",
            "task_id": task_id or uuid.uuid4().hex,
            "streaming": "duplex",
        },
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": model,
            "parameters": {"format": "pcm", "sample_rate": 16000, "language": language},
            "input": {},
        },
    }


def asr_audio_frame(task_id: str, pcm: bytes) -> dict:
    return {
        "header": {"action": "continue-task", "task_id": task_id},
        "payload": {"input": {"audio": pcm.hex()}},
    }


def parse_asr_event(message: dict) -> tuple[str, bool]:
    header = message.get("header") or {}
    payload = message.get("payload") or {}
    output = payload.get("output") or {}
    sentence = output.get("sentence")
    if isinstance(sentence, dict):
        text = (sentence.get("text") or "").strip()
        ended = bool(sentence.get("sentence_end"))
    else:
        raw = sentence or output.get("text") or ""
        text = raw.strip() if isinstance(raw, str) else ""
        ended = bool(output.get("sentence_end"))
    event = header.get("event")
    finished = event == "task-finished" or (event == "result-generated" and ended)
    return text, finished


def tts_request(model: str, text: str, *, voice: str = "") -> dict:
    body = {
        "model": model,
        "input": {"text": text},
        "parameters": {"format": "pcm", "sample_rate": 24000},
    }
    if voice:
        body["parameters"]["voice"] = voice
    return body


def parse_tts_response(payload: dict) -> bytes:
    audio = (payload.get("output") or {}).get("audio") or ""
    if not audio:
        raise ValueError("dashscope tts response has no audio")
    return bytes.fromhex(audio)


def realtime_session(model: str) -> dict:
    return {
        "type": "session.update",
        "session": {
            "model": model,
            "modalities": ["text", "audio"],
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
        },
    }


def authorization_header(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def dumps(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def _finish_task(task_id: str) -> dict:
    return {
        "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
        "payload": {"input": {}},
    }


async def transcribe_pcm(
    api_key: str,
    model: str,
    pcm: bytes,
    *,
    url: str,
    sample_rate: int = 16000,
    language: str = "zh",
) -> str:
    """Open the Bailian ASR websocket and return the final transcript."""
    import asyncio

    import websockets

    task = asr_run_task(model, language=language or "zh")
    task_id = task["header"]["task_id"]
    finals: list[str] = []
    latest = ""
    async with websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        open_timeout=20,
        max_size=8_000_000,
    ) as ws:
        await ws.send(dumps(task))
        started = False
        for _ in range(6):
            raw = await asyncio.wait_for(ws.recv(), 15)
            if not isinstance(raw, str):
                continue
            event = json.loads(raw)
            name = (event.get("header") or {}).get("event")
            if name == "task-started":
                started = True
                break
            if name == "task-failed":
                raise RuntimeError(raw[:400])
        if not started:
            raise RuntimeError("dashscope asr did not start")
        for offset in range(0, len(pcm), 3200):
            await ws.send(pcm[offset : offset + 3200])
        await ws.send(dumps(_finish_task(task_id)))
        for _ in range(40):
            raw = await asyncio.wait_for(ws.recv(), 15)
            if not isinstance(raw, str):
                continue
            event = json.loads(raw)
            text, ended = parse_asr_event(event)
            if text:
                latest = text
            if ended and text:
                finals.append(text)
            if (event.get("header") or {}).get("event") in {"task-finished", "task-failed"}:
                if (event.get("header") or {}).get("event") == "task-failed":
                    raise RuntimeError(raw[:400])
                break
    return "".join(finals) or latest


def _download_tts(api_key: str, url: str, body: dict) -> bytes:
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode())
    audio = (payload.get("output") or {}).get("audio") or {}
    audio_url = audio.get("url") if isinstance(audio, dict) else ""
    if not audio_url:
        raise RuntimeError("dashscope tts response has no audio url")
    with urllib.request.urlopen(audio_url, timeout=60) as audio_resp:
        return audio_resp.read()


async def synthesize_speech(
    api_key: str,
    model: str,
    text: str,
    *,
    url: str,
    voice: str = "",
) -> bytes:
    """Call Bailian SpeechSynthesizer and return the WAV bytes."""
    import asyncio

    body = {
        "model": model,
        "input": {
            "text": text,
            "voice": voice or "longanhuan_v3.1",
            "format": "wav",
            "sample_rate": 24000,
        },
    }
    return await asyncio.to_thread(_download_tts, api_key, url, body)
