from contextlib import asynccontextmanager
import os
from pathlib import Path
import types

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
import ffmpeg
from pyannote.audio import Pipeline
import torch
import whisperx

from srt import create_srt
from transcription import get_prompt, transcribe
from viewer import create_viewer

load_dotenv()

ONLINE = os.getenv("ONLINE") == "True"
DEVICE = os.getenv("DEVICE")
ROOT = os.getenv("ROOT")
BATCH_SIZE = int(os.getenv("BATCH_SIZE"))

model = None
diarize_model = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, diarize_model
    
    compute_type = "float16" if DEVICE != "cpu" else "float32"
    model = whisperx.load_model(
        "large-v3",
        DEVICE,
        compute_type=compute_type,
        download_root="models/whisperx",
    )

    model.model.get_prompt = types.MethodType(get_prompt, model.model)
    diarize_model = Pipeline.from_pretrained(
        "pyannote/speaker-diarization", use_auth_token=os.getenv("HF_AUTH_TOKEN")
    ).to(torch.device(DEVICE))

    # Create necessary directories
    for directory in [
        Path(ROOT + "data/in/"),
        Path(ROOT + "data/out/"),
        Path(ROOT + "data/error/"),
        Path(ROOT + "data/worker/"),
    ]:
        directory.mkdir(parents=True, exist_ok=True)

    yield
    del model, diarize_model


app = FastAPI(lifespan=lifespan)


@app.post("/transcribe")
async def transcribe_audio(audio_file: UploadFile = File(...), hotwords: list[str] = Form(default=[])):
    try:
        # Create temporary file to store the uploaded audio
        temp_file_path = Path(ROOT + f"temp_{audio_file.filename}")
        with temp_file_path.open("wb") as temp_file:
            content = await audio_file.read()
            temp_file.write(content)

        # Verify audio stream exists
        if not ffmpeg.probe(temp_file_path, select_streams="a")["streams"]:
            temp_file_path.unlink()
            return {"error": "No valid audio stream found in the file"}

        # Process audio file
        output_file = Path(ROOT + f"temp_processed_{audio_file.filename}")
        exit_status = os.system(
            f'ffmpeg -y -i "{temp_file_path}" -filter:v scale=320:-2 -af "lowpass=3000,highpass=200" "{output_file}"'
        )

        if exit_status == 256:
            exit_status = os.system(
                f'ffmpeg -y -i "{temp_file_path}" -c:v copy -af "lowpass=3000,highpass=200" "{output_file}"'
            )

        if exit_status != 0:
            output_file = temp_file_path

        # Perform transcription
        data = transcribe(
            output_file,
            model,
            diarize_model,
            DEVICE,
            None,
            add_language=True,
            hotwords=hotwords,
            batch_size=BATCH_SIZE,
        )

        # Generate SRT and viewer content
        srt_content = create_srt(data)
        viewer_content = create_viewer(data, output_file, encode_base64=True, combine_speaker=False, root=ROOT)

        # Clean up temporary files
        temp_file_path.unlink()
        if output_file != temp_file_path:
            output_file.unlink()

        return {"transcription": data, "srt": srt_content, "viewer": viewer_content}

    except Exception as e:
        # Clean up temporary files in case of error
        if temp_file_path.exists():
            temp_file_path.unlink()
        if output_file.exists() and output_file != temp_file_path:
            output_file.unlink()

        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
