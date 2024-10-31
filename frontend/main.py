import base64
import datetime
import os
import shutil
import time
import zipfile
from functools import partial
from os import listdir
from os.path import isfile, join

import aiofiles
import aiohttp
from dotenv import load_dotenv
from file import FileStatus
from help import help
from nicegui import app, events, ui
from util import time_estimate

load_dotenv()

STORAGE_SECRET = os.getenv("STORAGE_SECRET")
ROOT = os.getenv("ROOT")
API_URL = os.getenv("API_URL", "http://localhost:8000")


def initialize_storage() -> None:
    """Initialize storage if not already present"""
    if "file_list" not in app.storage.user:
        app.storage.user["file_list"] = []
    if "known_errors" not in app.storage.user:
        app.storage.user["known_errors"] = []
    if "updates" not in app.storage.user:
        app.storage.user["updates"] = None
    if "file_in_progress" not in app.storage.user:
        app.storage.user["file_in_progress"] = None


async def handle_upload(e: events.UploadEventArguments, user_id, refresh_file_view):
    # Get hotwords if they exist
    hotwords = []
    if (
        user_id + "vocab" in app.storage.user
        and len(app.storage.user["vocab"].strip()) > 0
    ):
        hotwords = app.storage.user["vocab"].strip().split("\n")

    if not os.path.exists(ROOT + "data/in/" + user_id):
        os.makedirs(ROOT + "data/in/" + user_id)
    if not os.path.exists(ROOT + "data/out/" + user_id):
        os.makedirs(ROOT + "data/out/" + user_id)

    file_name = e.name

    # Handle existing files in error directory
    if os.path.exists(ROOT + "data/error/" + user_id):
        if file_name in app.storage.user["known_errors"]:
            app.storage.user["known_errors"] = app.storage.user["known_errors"].remove(
                file_name
            )
        if os.path.exists(join(ROOT + "data/error/" + user_id, file_name)):
            os.remove(join(ROOT + "data/error/" + user_id, file_name))
        if os.path.exists(join(ROOT + "data/error/" + user_id, file_name + ".txt")):
            os.remove(join(ROOT + "data/error/" + user_id, file_name + ".txt"))

    # Handle duplicate filenames
    for i in range(10000):
        if isfile(ROOT + "data/in/" + user_id + "/" + file_name):
            file_name = (
                ".".join(e.name.split(".")[:-1])
                + f"_{str(i)}."
                + "".join(e.name.split(".")[-1:])
            )

    try:
        # Update UI to show processing status
        app.storage.user["updates"] = FileStatus(
            filename=file_name,
            status_message="Datei wird verarbeitet...",
            progress_percentage=50.0,
            estimated_time_remaining=0,
            last_modified=time.time(),
        )

        # Send file to API
        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()
            data.add_field("audio_file", e.content.read(), filename=file_name)
            for word in hotwords:
                data.add_field("hotwords", word)

            async with session.post(f"{API_URL}/transcribe", data=data) as response:
                if response.status == 200:
                    result = await response.json()

                    # Save results
                    out_path = ROOT + "data/out/" + user_id + "/"

                    # Save transcription data
                    async with aiofiles.open(out_path + file_name + ".json", "w") as f:
                        await f.write(str(result["transcription"]))

                    # Save SRT
                    async with aiofiles.open(out_path + file_name + ".srt", "w") as f:
                        await f.write(result["srt"])

                    # Save viewer HTML
                    async with aiofiles.open(out_path + file_name + ".html", "w") as f:
                        await f.write(result["viewer"])

                    # Save audio file
                    async with aiofiles.open(out_path + file_name + ".mp4", "wb") as f:
                        await f.write(e.content.read())

                    # Update UI to show completion
                    app.storage.user["updates"] = FileStatus(
                        filename=file_name,
                        status_message="Datei transkribiert",
                        progress_percentage=100.0,
                        estimated_time_remaining=0,
                        last_modified=time.time(),
                    )

                else:
                    # Handle error
                    error_msg = await response.text()
                    if not os.path.exists(ROOT + "data/error/" + user_id):
                        os.makedirs(ROOT + "data/error/" + user_id)
                    async with aiofiles.open(
                        ROOT + "data/error/" + user_id + "/" + file_name + ".txt", "w"
                    ) as f:
                        await f.write(error_msg)
                    app.storage.user["known_errors"] = app.storage.user.get(
                        "known_errors", []
                    )
                    app.storage.user["known_errors"] = app.storage.user[
                        "known_errors"
                    ] + [file_name]

    except Exception as e:
        # Handle connection/processing errors
        if not os.path.exists(ROOT + "data/error/" + user_id):
            os.makedirs(ROOT + "data/error/" + user_id)
        async with aiofiles.open(
            ROOT + "data/error/" + user_id + "/" + file_name + ".txt", "w"
        ) as f:
            await f.write(str(e))
        app.storage.user["known_errors"] = app.storage.user.get("known_errors", []) + [
            file_name
        ]

    # Force refresh of the file view
    refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=False)


# Read in all files of the user and set the file status if known.
def read_files(user_id):
    file_list: list[FileStatus] = []

    # Process files in input directory
    if os.path.exists(ROOT + "data/in/" + user_id):
        user_path = ROOT + "data/in/" + user_id
        for f in listdir(user_path):
            if isfile(join(user_path, f)) and not f == "hotwords.txt":
                last_modified = os.path.getmtime(join(user_path, f))

                # Check if file is already transcribed
                if isfile(join(ROOT + "data/out/" + user_id, f + ".html")):
                    file_status = FileStatus.create_completed(f, last_modified)
                else:
                    # Calculate estimated transcription time
                    estimated_time, _ = time_estimate(join(user_path, f))
                    if estimated_time == -1:
                        estimated_time = 0
                    file_status = FileStatus.create_queued(
                        f, last_modified, estimated_time
                    )

                file_list.append(file_status)

    # Process files in error directory
    if os.path.exists(ROOT + "data/error/" + user_id):
        user_path = ROOT + "data/error/" + user_id
        for f in listdir(user_path):
            if isfile(join(user_path, f)) and ".txt" not in f:
                error_message = "Transkription fehlgeschlagen"
                try:
                    with open(join(user_path, f) + ".txt", "r") as txtf:
                        content = txtf.read()
                        if content:
                            error_message = content
                except:
                    pass

                last_modified = os.path.getmtime(join(user_path, f))
                file_status = FileStatus.create_error(f, last_modified, error_message)

                known_errors = app.storage.user.get("known_errors", [])
                if f not in known_errors:
                    known_errors += [f]
                    app.storage.user["known_errors"] = known_errors

                file_list.append(file_status)

    # Calculate waiting times for queued files
    files_in_queue = [f for f in file_list if f.progress_percentage < 100.0]
    for file_status in file_list:
        if file_status.progress_percentage < 100.0:
            estimated_wait_time = sum(
                f.estimated_time_remaining
                for f in files_in_queue
                if f.last_modified < file_status.last_modified
            )
            file_status.status_message += str(
                datetime.timedelta(
                    seconds=round(
                        estimated_wait_time + file_status.estimated_time_remaining
                    )
                )
            )

    # Sort files by progress (completed last), then by modification time (newest first), then by name
    sorted_file_list = sorted(
        file_list, key=lambda x: (x.progress_percentage, -x.last_modified, x.filename)
    )

    # Update storage
    app.storage.user["file_list"] = sorted_file_list


def handle_reject(e: events.GenericEventArguments):
    ui.notify(
        "Ungültige Datei. Es können nur Audio/Video-Dateien unter 12GB transkribiert werden."
    )


# After a file was added, refresh the gui.
def handle_added(
    e: events.GenericEventArguments, user_id, upload_element, refresh_file_view
):
    upload_element.run_method("removeUploadedFiles")
    refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=False)


# Add offline functions to the editor before downloading.
def prepare_download(file_name, user_id):
    full_file_name = join(ROOT + "data/out/" + user_id, file_name + ".html")

    with open(full_file_name, "r", encoding="utf-8") as f:
        content = f.read()
    if os.path.exists(full_file_name + "update"):
        with open(full_file_name + "update", "r", encoding="utf-8") as f:
            new_content = f.read()
        start_index = content.find("</nav>") + len("</nav>")
        end_index = content.find("var fileName = ")

        content = content[:start_index] + new_content + content[end_index:]

        with open(full_file_name, "w", encoding="utf-8") as f:
            f.write(content)

        os.remove(full_file_name + "update")

    content = content.replace(
        "<div>Bitte den Editor herunterladen, um den Viewer zu erstellen.</div>",
        '<a href="#" id="viewer-link" onclick="viewerClick()" class="btn btn-primary">Viewer erstellen</a>',
    )
    if not "var base64str = " in content:
        with open(
            join(ROOT + "data/out/" + user_id, file_name + ".mp4"), "rb"
        ) as videoFile:
            video_base64 = base64.b64encode(videoFile.read()).decode("utf-8")

        video_content = f'var base64str = "{video_base64}";'
        video_content += """
var binary = atob(base64str);
var len = binary.length;
var buffer = new ArrayBuffer(len);
var view = new Uint8Array(buffer);
for (var i = 0; i < len; i++) {
    view[i] = binary.charCodeAt(i);
}
              
var blob = new Blob( [view], { type: "video/MP4" });

var url = URL.createObjectURL(blob);

var video = document.getElementById("player")

setTimeout(function() {
  video.pause();
  video.setAttribute('src', url);
}, 100);
</script>
"""
        content = content.replace("</script>", video_content)

    with open(full_file_name + "final", "w", encoding="utf-8") as f:
        f.write(content)


async def download_editor(file_name, user_id):
    prepare_download(file_name, user_id)
    ui.download(
        src=join(ROOT + "data/out/" + user_id, file_name + ".htmlfinal"),
        filename=file_name.split(".")[0] + ".html",
    )


async def download_srt(file_name, user_id):
    ui.download(
        src=join(ROOT + "data/out/" + user_id, file_name + ".srt"),
        filename=file_name.split(".")[0] + ".srt",
    )


async def open_editor(file_name: str, user_id: str) -> None:
    """
    Open the editor for a specific file in a new tab.

    Args:
        file_name: Name of the file to edit
        user_id: ID of the current user
    """
    full_file_name = join(ROOT + "data/out/" + user_id, file_name + ".html")

    try:
        with open(full_file_name, "r", encoding="utf-8") as f:
            content = f.read()

            # Update video source paths
            content = content.replace(
                '<video id="player" width="100%" style="max-height: 320px" src="" type="video/MP4" controls="controls" position="sticky"></video>',
                f'<video id="player" width="100%" style="max-height: 320px" src="/data/{user_id}/{file_name}.mp4" type="video/MP4" controls="controls" position="sticky"></video>',
            )
            content = content.replace(
                '<video id="player" width="100%" style="max-height: 250px" src="" type="video/MP4" controls="controls" position="sticky"></video>',
                f'<video id="player" width="100%" style="max-height: 250px" src="/data/{user_id}/{file_name}.mp4" type="video/MP4" controls="controls" position="sticky"></video>',
            )

            # Store content and file information in user storage
            app.storage.user["editor_content"] = content
            app.storage.user["editor_file"] = full_file_name

            # Open editor in new tab
            ui.navigate.to(editor, new_tab=True)

    except Exception as e:
        ui.notify(f"Error opening editor: {str(e)}", color="negative")


async def download_all(user_id: str) -> None:
    """
    Create a zip file containing all completed transcriptions for a user.

    Args:
        user_id: ID of the current user
    """
    zip_path = join(ROOT, "data/out", user_id, "transcribed_files.zip")

    try:
        with zipfile.ZipFile(zip_path, "w", allowZip64=True) as myzip:
            file_list = app.storage.user.get("file_list", [])

            for file_status in file_list:
                # Only include completed transcriptions
                if file_status.progress_percentage == 100.0:
                    # Prepare the file for download (adds offline functionality)
                    prepare_download(file_status.filename, user_id)

                    # Add prepared file to zip
                    source_path = join(
                        ROOT, "data/out", user_id, f"{file_status.filename}.htmlfinal"
                    )
                    archive_path = f"{file_status.filename}.html"
                    myzip.write(source_path, archive_path)

        # Trigger download of the zip file
        ui.download(zip_path)

    except Exception as e:
        ui.notify(f"Error creating zip file: {str(e)}", color="negative")


def delete(file_name, user_id, refresh_file_view):
    if os.path.exists(join(ROOT + "data/in/" + user_id, file_name)):
        os.remove(join(ROOT + "data/in/" + user_id, file_name))
    for suffix in ["", ".txt", ".html", ".mp4", ".srt"]:
        if os.path.exists(join(ROOT + "data/out/" + user_id, file_name + suffix)):
            os.remove(join(ROOT + "data/out/" + user_id, file_name + suffix))
    if os.path.exists(join(ROOT + "data/error/" + user_id, file_name)):
        os.remove(join(ROOT + "data/error/" + user_id, file_name))
    if os.path.exists(join(ROOT + "data/error/" + user_id, file_name + ".txt")):
        os.remove(join(ROOT + "data/error/" + user_id, file_name + ".txt"))
    if os.path.exists(join(ROOT + "data/out/" + user_id, file_name + ".htmlupdate")):
        os.remove(join(ROOT + "data/out/" + user_id, file_name + ".htmlupdate"))

    refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=True)


# Periodically check if a file is being transcribed and calulate its estimated progress.
def listen(user_id, refresh_file_view):
    user_path = ROOT + "data/worker/" + user_id + "/"

    if os.path.exists(user_path):
        for f in listdir(user_path):
            if isfile(join(user_path, f)):
                f = f.split("_")
                estimated_time = f[0]
                start = f[1]
                file_name = "_".join(f[2:])
                progress = min(
                    0.975, (time.time() - float(start)) / float(estimated_time)
                )
                estimated_time_left = round(
                    max(1, float(estimated_time) - (time.time() - float(start)))
                )
                if os.path.exists(join(ROOT + "data/in/" + user_id + "/", file_name)):
                    app.storage.user["updates"] = FileStatus(
                        filename=file_name,
                        status_message=f"Datei wird transkribiert. Geschätzte Bearbeitungszeit: {str(datetime.timedelta(seconds=estimated_time_left))}",
                        progress_percentage=progress * 100,
                        estimated_time_remaining=estimated_time_left,
                        last_modified=os.path.getmtime(
                            join(ROOT + "data/in/" + user_id + "/", file_name)
                        ),
                    )
                else:
                    os.remove(join(user_path, "_".join(f)))
                refresh_file_view(
                    user_id=user_id,
                    refresh_queue=True,
                    refresh_results=app.storage.user.get("file_in_progress") is not None
                    and not app.storage.user.get("file_in_progress") == file_name,
                )
                app.storage.user["file_in_progress"] = file_name
                return

        if app.storage.user.get("updates"):
            app.storage.user["updates"] = None
            app.storage.user["file_in_progress"] = None
            refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=True)
        else:
            refresh_file_view(
                user_id=user_id, refresh_queue=True, refresh_results=False
            )


def update_hotwords(user_id: str) -> None:
    """
    Update the user's custom vocabulary/hotwords in storage.

    Args:
        user_id: ID of the current user
    """
    # Get the textarea value from storage
    textarea_value = app.storage.user.get("textarea_value", "")

    # Store the vocabulary in user storage
    app.storage.user["vocab"] = textarea_value


# Prepare and open the editor for online editing.
@ui.page("/editor")
async def editor():
    async def handle_save(full_file_name: str) -> None:
        content = ""
        for i in range(100):
            content_tmp = await ui.run_javascript(
                """
                var content = String(document.documentElement.innerHTML);
                var start_index = content.indexOf('<!--start-->') + '<!--start-->'.length;
                content = content.slice(start_index, content.indexOf('var fileName = ', start_index))
                content = content.slice(content.indexOf('</nav>') + '</nav>'.length, content.length)
                return content.slice("""
                + str(i * 500_000)
                + ","
                + str(((i + 1) * 500_000))
                + ")",
                timeout=60.0,
            )
            content += content_tmp
            if len(content_tmp) < 500_000:
                break

        with open(full_file_name + "update", "w", encoding="utf-8") as f:
            f.write(content.strip())

        ui.notify("Änderungen gespeichert.")

    user_id = str(app.storage.browser["id"])
    app.add_media_files("/data/" + user_id, join(ROOT + "data/out/" + user_id))

    editor_content = app.storage.user.get("editor_content")
    editor_file = app.storage.user.get("editor_file")

    if editor_content and editor_file:
        full_file_name = editor_file
        ui.on("editor_save", lambda e: handle_save(full_file_name))
        ui.add_body_html("<!--start-->")

        if os.path.exists(full_file_name + "update"):
            with open(full_file_name + "update", "r", encoding="utf-8") as f:
                new_content = f.read()
            start_index = editor_content.find("</nav>") + len("</nav>")
            end_index = editor_content.find("var fileName = ")
            editor_content = (
                editor_content[:start_index] + new_content + editor_content[end_index:]
            )

        editor_content = editor_content.replace(
            '<a href="#" id="viewer-link" onclick="viewerClick()" class="btn btn-primary">Viewer erstellen</a>',
            "<div>Bitte den Editor herunterladen, um den Viewer zu erstellen.</div>",
        )
        ui.add_body_html(editor_content)

        ui.add_body_html("""<script language="javascript">
            var origFunction = downloadClick;
            downloadClick = function downloadClick() {
                emitEvent('editor_save');
            }
        </script>""")
    else:
        ui.label("Session abgelaufen. Bitte öffne den Editor erneut.")


@ui.page("/")
async def main_page():
    @ui.refreshable
    def display_queue(user_id: str) -> None:
        """Display files that are currently in queue or being processed"""
        file_list = app.storage.user.get("file_list", [])
        current_updates = app.storage.user.get("updates")

        for file_status in sorted(
            file_list,
            key=lambda x: (x.progress_percentage, -x.last_modified, x.filename),
        ):
            # Update status if file is being processed
            if (
                current_updates
                and current_updates.filename == file_status.filename
                and file_status.progress_percentage < 100.0
            ):
                file_status = current_updates

            if 0 <= file_status.progress_percentage < 100.0:
                ui.markdown(
                    f"<b>{file_status.filename.replace('_', '\\_')}</b>: {file_status.status_message}"
                )
                ui.linear_progress(
                    value=file_status.progress_percentage / 100,
                    show_value=False,
                    size="10px",
                ).props("instant-feedback")
                ui.separator()

    @ui.refreshable
    def display_results(user_id: str) -> None:
        """Display completed and failed transcriptions"""
        file_list = app.storage.user.get("file_list", [])
        current_updates = app.storage.user.get("updates")
        any_file_ready = False

        for file_status in sorted(
            file_list,
            key=lambda x: (x.progress_percentage, -x.last_modified, x.filename),
        ):
            # Update status if file is being processed
            if (
                current_updates
                and current_updates.filename == file_status.filename
                and file_status.progress_percentage < 100.0
            ):
                file_status = current_updates

            if file_status.progress_percentage == 100.0:
                # Display completed files
                ui.markdown(f"<b>{file_status.filename.replace('_', '\\_')}</b>")
                with ui.row():
                    ui.button(
                        "Editor herunterladen (Lokal)",
                        on_click=lambda f=file_status.filename: download_editor(
                            f, user_id
                        ),
                    ).props("no-caps")
                    ui.button(
                        "Editor öffnen (Server)",
                        on_click=lambda f=file_status.filename: open_editor(f, user_id),
                    ).props("no-caps")
                    ui.button(
                        "SRT-Datei",
                        on_click=lambda f=file_status.filename: download_srt(
                            f, user_id
                        ),
                    ).props("no-caps")
                    ui.button(
                        "Datei entfernen",
                        on_click=lambda f=file_status.filename: delete(
                            f, user_id, refresh_file_view
                        ),
                        color="red-5",
                    ).props("no-caps")
                    any_file_ready = True
                ui.separator()

            elif file_status.progress_percentage == -1:
                # Display failed files
                ui.markdown(
                    f"<b>{file_status.filename.replace('_', '\\_')}</b>: {file_status.status_message}"
                )
                ui.button(
                    "Datei entfernen",
                    on_click=lambda f=file_status.filename: delete(
                        f, user_id, refresh_file_view
                    ),
                    color="red-5",
                ).props("no-caps")
                ui.separator()

        if any_file_ready:
            ui.button(
                "Alle Dateien herunterladen", on_click=lambda: download_all(user_id)
            ).props("no-caps")

    def refresh_file_view(
        user_id: str, refresh_queue: bool, refresh_results: bool
    ) -> None:
        """Refresh the file view UI components"""
        known_errors = app.storage.user.get("known_errors", [])
        num_errors = len(known_errors)

        read_files(user_id)

        if refresh_queue:
            display_queue.refresh(user_id=user_id)
        if refresh_results or num_errors < len(
            app.storage.user.get("known_errors", [])
        ):
            display_results.refresh(user_id=user_id)

    def display_files(user_id: str) -> None:
        """Display all files with their current status"""
        read_files(user_id)

        with ui.card().classes("border p-4").style("width: min(60vw, 700px);"):
            display_queue(user_id=user_id)
            display_results(user_id=user_id)

    # Initialize page
    user_id = str(app.storage.browser["id"])

    # Clean up temporary directory if it exists
    tmp_path = join(ROOT, "data/in", user_id, "tmp")
    if os.path.exists(tmp_path):
        shutil.rmtree(tmp_path)

    # Initialize storage and read files
    initialize_storage()
    read_files(user_id)

    # Create the GUI
    # Header should be at the top level, not inside a column
    with ui.header(elevated=True).style("background-color: #0070b4;").props(
        "fit=scale-down"
    ).classes("q-pa-xs-xs"):
        ui.image(ROOT + "assets/data/banner.png").style("height: 90px; width: 443px;")

    # Main content in a column
    with ui.column():
        with ui.row():
            # Left column: Upload and controls
            with ui.column():
                with ui.card().classes("border p-4").style("width: min(40vw, 400px)"):
                    upload_element = (
                        ui.upload(
                            multiple=True,
                            on_upload=lambda e: handle_upload(
                                e, user_id, refresh_file_view
                            ),
                            on_rejected=handle_reject,
                            label="Dateien auswählen",
                            auto_upload=True,
                            max_file_size=12_000_000_000,
                            max_files=100,
                        )
                        .props('accept="video/*, audio/*"')
                        .tooltip("Dateien auswählen")
                        .classes("w-full")
                        .style("width: 100%;")
                    )
                    upload_element.on(
                        "uploaded",
                        lambda e: handle_added(
                            e, user_id, upload_element, refresh_file_view
                        ),
                    )

                ui.label("")
                ui.timer(2.0, lambda: listen(user_id, refresh_file_view))

                # Vocabulary section
                with ui.expansion("Vokabular", icon="menu_book").classes(
                    "w-full no-wrap"
                ).style("width: min(40vw, 400px)") as expansion:
                    ui.textarea(
                        label="Vokabular",
                        placeholder="Basel\nBasel Stadt\nBasilea",
                        on_change=lambda e: update_textarea_value(e.value, user_id),
                        value=app.storage.user.get("vocab", ""),
                    ).classes("w-full h-full")

                    if app.storage.user.get("vocab"):
                        expansion.open()

                # Information section
                with ui.expansion("Informationen", icon="help_outline").classes(
                    "w-full no-wrap"
                ).style("width: min(40vw, 400px)"):
                    ui.label(
                        "Diese Prototyp-Applikation wurde vom Statistischen Amt Kanton Zürich entwickelt."
                    )

                ui.button(
                    "Anleitung öffnen", on_click=lambda: ui.navigate.to(help, new_tab=True)
                ).props("no-caps")

            # Right column: File display
            display_files(user_id=user_id)


def update_textarea_value(value: str, user_id: str) -> None:
    """Update the vocabulary textarea value in storage"""
    app.storage.user["vocab"] = value
    update_hotwords(user_id)


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        port=8080,
        title="Transcribo",
        storage_secret=STORAGE_SECRET,
        favicon=ROOT + "assets/data/logo.png",
        language="de-CH",
    )
