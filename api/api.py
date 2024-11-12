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

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from queue import Queue
import asyncio
import time
import uuid
from typing import Dict

# Add these at the top with other imports
from dataclasses import dataclass
from datetime import datetime

load_dotenv()

ONLINE = os.getenv("ONLINE") == "True"
DEVICE = os.getenv("DEVICE")
ROOT = os.getenv("ROOT")
BATCH_SIZE = int(os.getenv("BATCH_SIZE"))
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE"))
PROCESSING = False
queue_full_message = "Queue is full. Please try again later."

model = None
diarize_model = None


@dataclass
class QueueItem:
    id: str
    file_name: str
    file_content: bytes
    hotwords: list[str]
    timestamp: datetime
    status: str = "queued"  # queued, processing, completed, failed
    result: dict = None
    position: int = 0
    audio_length: float = 0.0


request_queue = Queue()
active_requests: Dict[str, QueueItem] = {}



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

    for directory in [
        Path(ROOT + "data/in/"),
        Path(ROOT + "data/out/"),
        Path(ROOT + "data/error/"),
        Path(ROOT + "data/worker/"),
    ]:
        directory.mkdir(parents=True, exist_ok=True)

    asyncio.create_task(process_queue())

    yield
    del model, diarize_model


app = FastAPI(lifespan=lifespan)

async def get_audio_length(file_content: bytes, temp_file_path: Path) -> float:
    # Write temporary file to get duration
    with temp_file_path.open("wb") as temp_file:
        temp_file.write(file_content)
    
    # Get audio duration using ffmpeg
    probe = ffmpeg.probe(str(temp_file_path))
    duration = float(probe['streams'][0]['duration'])
    return duration

async def process_queue():
    global PROCESSING
    while True:
        if not request_queue.empty() and not PROCESSING:
            PROCESSING = True
            item: QueueItem = request_queue.get()
            try:
                item.status = "processing"
                item.position = 0
                active_requests[item.id] = item

                # Create temporary file
                temp_file_path = Path(ROOT + f"temp_{item.file_name}")
                with temp_file_path.open("wb") as temp_file:
                    temp_file.write(item.file_content)

                # Process the transcription request
                result = await process_transcription(temp_file_path, item.hotwords)

                item.status = "completed"
                item.result = result

                # Cleanup
                if temp_file_path.exists():
                    temp_file_path.unlink()

            except Exception as e:
                item.status = "failed"
                item.result = {"error": str(e)}
                active_requests[item.id] = item
            finally:
                PROCESSING = False

                # Cleanup temporary file
                if temp_file_path.exists():
                    temp_file_path.unlink()

                # Update positions and estimated wait times for remaining queued items
                total_wait_time = 0.0
                for queued_item in list(active_requests.values()):
                    if queued_item.status == "queued":
                        # Each 10 seconds of audio takes ~1 second to process
                        processing_time = queued_item.audio_length / 10
                        queued_item.position -= 1
                        queued_item.estimated_wait_time = total_wait_time
                        total_wait_time += processing_time

        await asyncio.sleep(1)


@app.post("/transcribe")
async def transcribe_audio(
    audio_file: UploadFile = File(...), hotwords: list[str] = Form(default=[])
):
    if request_queue.qsize() >= MAX_QUEUE_SIZE:
        raise HTTPException(
            status_code=503,  # Service Unavailable
            detail=queue_full_message
        )
    
    # Generate unique ID for this request
    request_id = str(uuid.uuid4())
    file_content = await audio_file.read()
    
    # Create temporary file to get audio length
    temp_file_path = Path(ROOT + f"temp_{audio_file.filename}")
    audio_length = await get_audio_length(file_content, temp_file_path)
    
    # Cleanup temp file
    if temp_file_path.exists():
        temp_file_path.unlink()

    # Create queue item
    item = QueueItem(
        id=request_id,
        file_name=audio_file.filename,
        file_content=file_content,
        hotwords=hotwords,
        timestamp=datetime.now(),
        audio_length=audio_length
    )

    # Calculate waiting time based on items in queue
    total_wait_time = 0
    for queued_item in list(active_requests.values()):
        if queued_item.status == "queued":
            # Each 10 seconds of audio takes ~1 second to process
            total_wait_time += queued_item.audio_length / 10

    # Add current item's processing time
    processing_time = audio_length / 10

    # Update position
    item.position = request_queue.qsize() + (1 if PROCESSING else 0)

    # Add to queue and tracking dict
    request_queue.put(item)
    active_requests[request_id] = item

    return {
        "request_id": request_id,
        "position": item.position,
        "status": "queued",
        "estimated_wait_time": total_wait_time,
        "estimated_processing_time": processing_time
    }


@app.get("/status/{request_id}")
async def get_status(request_id: str):
    if request_id not in active_requests:
        raise HTTPException(status_code=404, detail="Request not found")

    item = active_requests[request_id]

    # Update position if still queued
    if item.status == "queued":
        item.position = list(active_requests.values()).index(item)

    response = {"status": item.status, "position": item.position}

    if item.status == "completed":
        response["result"] = item.result
        # Clean up completed request
        del active_requests[request_id]
    elif item.status == "failed":
        response["error"] = item.result["error"]
        del active_requests[request_id]

    return response


async def process_transcription(temp_file_path: Path, hotwords: list[str]):
    try:
        # Verify audio stream exists
        if not ffmpeg.probe(temp_file_path, select_streams="a")["streams"]:
            return {"error": "No valid audio stream found in the file"}

        # Process audio file
        output_file = Path(ROOT + f"temp_processed_{temp_file_path.name}")
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
        viewer_content = create_viewer(
            data, output_file, encode_base64=True, combine_speaker=False, root=ROOT
        )

        # Clean up temporary files
        if output_file != temp_file_path:
            output_file.unlink()

        return {"transcription": data, "srt": srt_content, "viewer": viewer_content}

    except Exception as e:
        if output_file.exists() and output_file != temp_file_path:
            output_file.unlink()
        raise e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
