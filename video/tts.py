"""Voice-over and word timings for the demo video (Microsoft neural voice via edge-tts).

    uv run --with edge-tts python video/tts.py

Writes public/<scene>.mp3 and public/timing.json: per scene, the audio length and every word with its start and end
in seconds. Remotion reads timing.json for scene lengths and subtitles.
"""
import asyncio
import json
from pathlib import Path

import edge_tts

HERE = Path(__file__).parent
VOICE = "en-US-AndrewMultilingualNeural"
RATE = "+4%"
SPOKEN = {"IROPS": "eye-rops", "DOT": "D.O.T.", "FAA": "F.A.A.", "MCP": "M.C.P.", "FalkorDB": "Falkor D.B."}


def norm(t):
    return "".join(c for c in t.lower() if c.isalnum())


def spoken(text):
    for k, v in SPOKEN.items():
        text = text.replace(k, v)
    return text


async def scene(s):
    out = HERE / "public" / f"{s['id']}.mp3"
    words, audio = [], bytearray()
    async for ch in edge_tts.Communicate(spoken(s["text"]), VOICE, rate=RATE, boundary="WordBoundary").stream():
        if ch["type"] == "audio":
            audio += ch["data"]
        elif ch["type"] == "WordBoundary":
            words.append({"w": ch["text"], "t0": ch["offset"] / 1e7, "t1": (ch["offset"] + ch["duration"]) / 1e7})
    out.write_bytes(audio)
    # Subtitles show the written words, not the phonetic spellings sent to the voice. Align on letters: each written
    # word goes to the boundary its first letter falls in (one boundary may span "December 2022", or one written
    # word may be voiced as two boundaries, "Falkor" + "D.B.").
    starts, pos = [], 0
    for w in words:
        starts.append(pos)
        pos += len(norm(w["w"]))
    texts, pos, j = [[] for _ in words], 0, 0
    for tok in s["text"].split():
        while j + 1 < len(starts) and starts[j + 1] <= pos:
            j += 1
        texts[j].append(tok)
        pos += len(norm(spoken(tok)))
    for w, t in zip(words, texts):
        w["w"] = " ".join(t)
    words = [w for w in words if w["w"]]
    return {"id": s["id"], "text": s["text"], "duration": words[-1]["t1"] + 0.35, "words": words}


async def main():
    (HERE / "public").mkdir(exist_ok=True)
    scenes = json.loads((HERE / "narration.json").read_text(encoding="utf-8"))
    timing = [await scene(s) for s in scenes]
    (HERE / "public" / "timing.json").write_text(json.dumps(timing, indent=1), encoding="utf-8")
    for t in timing:
        print(f"{t['id']:10} {t['duration']:5.1f}s {len(t['words'])} words")
    print(f"total {sum(t['duration'] for t in timing):.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
