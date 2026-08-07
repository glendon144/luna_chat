# Luna Chat Deluxe

A local Flask client for `gpt-5.6-luna`, retaining ECM-paced text, SQLite history, and the optional semantic cache while adding a small podcast studio.

## Requirements

- Python 3.10+
- An OpenAI API key
- FFmpeg (`brew install ffmpeg` on macOS)

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY='your-key'
python app.py
```

Open `http://127.0.0.1:5000`.

## Test

```bash
pip install -r requirements-dev.txt
pytest
```

## Audio features

- **Speak Aloud** generates and locally caches the selected Luna voice.
- The **Pacing** slider controls text speed and pitch-preserved audio playback speed.
- **Save Recording** exports an individual Luna response at the current paced speed.
- **Export Podcast** gives user prompts the Host voice and Luna responses the Luna voice. It now derives both voice tempo and inter-turn pause timing from the Pacing slider, normalizes the finished program to approximately -16 LUFS, and exports a VBR MP3 whose filename includes the applied speed.
- Synthetic voices should be disclosed as AI-generated when publishing audio.

Audio caches and exports are stored below `data/`.


## Transcript and response controls

- **Response Length** guides Luna from very concise through expansive answers.
- **Export Transcript** downloads exact conversation text as TXT and JSON in a ZIP archive.
- **Export Podcast** also writes TXT, JSON, and SRT sidecars in `data/exports/`.
- When **Verify exported podcast with OpenAI transcription** is enabled, the app sends the finished MP3 to `gpt-4o-transcribe` (configurable with `OPENAI_TRANSCRIBE_MODEL`) and stores a verification transcript beside the authoritative transcript.
