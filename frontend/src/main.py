import base64
import datetime
import os
import shutil
import time
import zipfile
from logging import Logger

import aiofiles
import aiohttp
from dotenv import load_dotenv
from file import FileStatus
from help import help
from nicegui import app, events, ui

logger = Logger(__name__)

load_dotenv()

STORAGE_SECRET = os.getenv("STORAGE_SECRET")
ROOT = os.getenv("ROOT")
API_URL = os.getenv("API_URL", "http://localhost:8000")


def initialize_storage() -> None:
    """Initialize storage if not already present"""
    app.storage.user["updates"] = app.storage.user.get("updates", {})
    file_status: FileStatus
    updates = app.storage.user["updates"]
    for idx, file_status in updates.items():
        if (
            os.path.exists(file_status.out_dir)
            and os.path.isdir(file_status.out_dir)
            and os.path.exists(os.path.join(file_status.out_dir, file_status.filename))
        ):
            continue
        else:
            updates.pop(idx)
    app.storage.user["editor_content"] = None
    app.storage.user["editor_files"] = None


async def handle_upload(e: events.UploadEventArguments, refresh_file_view):
    # Get hotwords if they exist
    hotwords = app.storage.user.get("vocab", "").strip().split("\n")

    user_id = str(app.storage.browser["id"])

    now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_dir = os.path.join(ROOT, "data/out/", user_id, now)
    error_dir = os.path.join(ROOT, "data/error/", user_id, now)
    os.makedirs(out_dir, exist_ok=True)

    file_name = e.name

    try:
        # Update UI to show processing status
        app.storage.user.get("updates")[out_dir] = FileStatus(
            filename=file_name,
            out_dir=out_dir,
            status_message="Datei wird hochgeladen...",
            progress_percentage=25.0,
            estimated_time_remaining=10,
            last_modified=time.time(),
        )

        # Send file to API
        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()
            content = e.content.read()
            data.add_field("audio_file", content, filename=file_name)
            for word in hotwords:
                data.add_field("hotwords", word)

            async with session.post(f"{API_URL}/transcribe", data=data) as response:
                # Update status of file
                app.storage.user.get("updates")[out_dir] = FileStatus(
                    filename=file_name,
                    out_dir=out_dir,
                    status_message="Datei wird verarbeitet...",
                    progress_percentage=50.0,
                    estimated_time_remaining=10,
                    last_modified=time.time(),
                )

                refresh_file_view(refresh_queue=True, refresh_results=True)

                if response.status == 200:
                    result = await response.json()

                    # Save transcription data
                    async with aiofiles.open(
                        os.path.join(out_dir, file_name + ".json"), "w"
                    ) as f:
                        await f.write(str(result["transcription"]))

                    # Save SRT
                    async with aiofiles.open(
                        os.path.join(out_dir, file_name + ".srt"), "w"
                    ) as f:
                        await f.write(result["srt"])

                    # Save viewer HTML
                    async with aiofiles.open(
                        os.path.join(out_dir, file_name + ".html"), "w"
                    ) as f:
                        await f.write(result["viewer"])

                    # Save audio file
                    async with aiofiles.open(
                        os.path.join(out_dir, file_name + ".mp4"), "wb"
                    ) as f:
                        await f.write(content)

                    # Update UI to show completion
                    app.storage.user.get("updates")[out_dir] = FileStatus(
                        filename=file_name,
                        out_dir=out_dir,
                        status_message="Datei transkribiert",
                        progress_percentage=100.0,
                        estimated_time_remaining=0,
                        last_modified=time.time(),
                    )

                    refresh_file_view(refresh_queue=True, refresh_results=True)

                else:
                    # Handle error
                    error_msg = await response.text()
                    async with aiofiles.open(
                        os.path.join(error_dir, file_name + ".txt"), "w"
                    ) as f:
                        await f.write(error_msg)

                    app.storage.user.get("updates")[out_dir].status_message = error_msg
                    app.storage.user.get("updates")[out_dir].progress_percentage = -1.0

    except Exception as e:
        # Handle connection/processing errors
        async with aiofiles.open(os.path.join(error_dir, file_name + ".txt"), "w") as f:
            await f.write(str(e))
        app.storage.user.get("updates")[out_dir].status_message = error_msg
        app.storage.user.get("updates")[out_dir].progress_percentage = -1.0

    # Force refresh of the file view
    refresh_file_view(refresh_queue=True, refresh_results=True)


def handle_reject(e: events.GenericEventArguments):
    ui.notify(
        "Ungültige Datei. Es können nur Audio/Video-Dateien unter 12GB transkribiert werden."
    )


# After a file was added, refresh the gui.
def handle_added(e: events.GenericEventArguments, upload_element, refresh_file_view):
    # upload_element.run_method("removeUploadedFiles")
    refresh_file_view(refresh_queue=True, refresh_results=False)


# Add offline functions to the editor before downloading.
def prepare_download(file_status: FileStatus):
    out_path = os.path.join(file_status.out_dir, file_status.filename)
    html_file_name = out_path + ".html"

    with open(html_file_name, "r", encoding="utf-8") as f:
        content = f.read()
    if os.path.exists(html_file_name + "update"):
        with open(html_file_name + "update", "r", encoding="utf-8") as f:
            new_content = f.read()
        start_index = content.find("</nav>") + len("</nav>")
        end_index = content.find("var fileName = ")

        content = content[:start_index] + new_content + content[end_index:]

        with open(html_file_name, "w", encoding="utf-8") as f:
            f.write(content)

        os.remove(html_file_name + "update")

    content = content.replace(
        "<div>Bitte den Editor herunterladen, um den Viewer zu erstellen.</div>",
        '<a href="#" id="viewer-link" onclick="viewerClick()" class="btn btn-primary">Viewer erstellen</a>',
    )
    if "var base64str = " not in content:
        with open(out_path + ".mp4", "rb") as videoFile:
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

    with open(html_file_name + "final", "w", encoding="utf-8") as f:
        f.write(content)


async def download_editor(file_status: FileStatus):
    prepare_download(file_status)
    file_path = os.path.join(file_status.out_dir, file_status.filename)
    ui.download(
        src=file_path + ".htmlfinal",
        filename=file_status.filename + ".html",
    )


async def download_srt(file_status: FileStatus):
    file_path = os.path.join(file_status.out_dir, file_status.filename)
    ui.download(
        src=file_path + ".srt",
        filename=file_status.filename.split(".")[0] + ".srt",
    )


async def open_editor(file_status: FileStatus) -> None:
    """
    Open the editor for a specific file in a new tab.
    """
    file_path = os.path.join(file_status.out_dir, file_status.filename)
    html_file_path = file_path + ".html"
    user_id = str(app.storage.browser["id"])

    try:
        with open(html_file_path, "r", encoding="utf-8") as f:
            content = f.read()

            # Update video source paths
            content = content.replace(
                '<video id="player" width="100%" style="max-height: 250px" src="" type="video/MP4" controls="controls" position="sticky"></video>',
                f'<video id="player" width="100%" style="max-height: 250px" src="/data/{user_id}/{file_status.filename}.mp4" type="video/MP4" controls="controls" position="sticky"></video>',
            )

            # Store content and file information in user storage
            app.storage.user["editor_content"] = content
            app.storage.user["editor_file"] = file_status

            # Open editor in new tab
            ui.navigate.to(editor, new_tab=True)

    except Exception as e:
        ui.notify(f"Error opening editor: {str(e)}", color="negative")


async def download_all() -> None:
    """
    Create a zip file containing all completed transcriptions for a user.
    """
    user_output_dir = os.path.dirname(
        list(app.storage.user["updates"].values())[0].out_dir
    )
    zip_path = os.path.join(user_output_dir, "transcribed_files.zip")

    try:
        with zipfile.ZipFile(zip_path, "w", allowZip64=True) as myzip:
            file_list = app.storage.user.get("updates").values()

            for file_status in file_list:
                # Only include completed transcriptions
                if file_status.progress_percentage == 100.0:
                    # Prepare the file for download (adds offline functionality)
                    prepare_download(file_status)

                    # Add prepared file to zip
                    source_path = os.path.join(
                        file_status.out_dir, file_status.filename + ".htmlfinal"
                    )
                    archive_path = f"{file_status.filename}.html"
                    myzip.write(source_path, archive_path)

        # Trigger download of the zip file
        ui.download(zip_path)
        time.sleep(1)
        shutil.rmtree(archive_path, True)

    except Exception as e:
        ui.notify(f"Error creating zip file: {str(e)}", color="negative")
        time.sleep(1)
        shutil.rmtree(archive_path, True)


def delete(file_status: FileStatus, refresh_file_view):
    out_dir = file_status.out_dir
    error_dir = out_dir.replace("out", "error")
    shutil.rmtree(out_dir, ignore_errors=True)
    shutil.rmtree(error_dir, ignore_errors=True)
    updates = app.storage.user["updates"]
    updates.pop(file_status.out_dir)
    app.storage.user["updates"] = updates
    refresh_file_view(refresh_queue=True, refresh_results=True)


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

    editor_content = app.storage.user.get("editor_content")
    editor_file: FileStatus = app.storage.user.get("editor_file")

    user_id = str(app.storage.browser["id"])
    app.add_media_files("/data/" + user_id, editor_file.out_dir)

    if editor_content and editor_file:
        full_file_name = os.path.join(editor_file.out_dir, editor_file.filename)
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
    def display_queue() -> None:
        """Display files that are currently in queue or being processed"""
        updates = app.storage.user.get("updates").values()

        file_status: FileStatus
        for file_status in updates:
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
    def display_results() -> None:
        """Display completed and failed transcriptions"""
        updates = app.storage.user.get("updates").values()
        any_file_ready = False
        file_status: FileStatus
        for file_status in updates:
            if file_status.progress_percentage == 100.0:
                ui.markdown(f"<b>{file_status.filename.replace('_', '\\_')}</b>")
                with ui.row():
                    ui.button(
                        "Editor herunterladen (Lokal)",
                        on_click=lambda f=file_status: download_editor(f),
                    ).props("no-caps")
                    ui.button(
                        "Editor öffnen (Server)",
                        on_click=lambda f=file_status: open_editor(f),
                    ).props("no-caps")
                    ui.button(
                        "SRT-Datei",
                        on_click=lambda f=file_status: download_srt(f),
                    ).props("no-caps")
                    ui.button(
                        "Datei entfernen",
                        on_click=lambda f=file_status: delete(f, refresh_file_view),
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
                    on_click=lambda f=file_status: delete(f, refresh_file_view),
                    color="red-5",
                ).props("no-caps")
                ui.separator()

        if any_file_ready:
            ui.button(
                "Alle Dateien herunterladen", on_click=lambda: download_all()
            ).props("no-caps")

    def refresh_file_view(refresh_queue: bool, refresh_results: bool) -> None:
        """Refresh the file view UI components"""
        known_errors = [
            status
            for status in app.storage.user.get("updates").values()
            if status.progress_percentage == -1.0
        ]
        num_errors = len(known_errors)

        if refresh_queue:
            display_queue.refresh()
        if refresh_results or num_errors > 0:
            display_results.refresh()

    def display_files() -> None:
        """Display all files with their current status"""
        with ui.card().classes("border p-4").style("width: min(60vw, 700px);"):
            display_queue()
            display_results()

    # Initialize storage and read files
    initialize_storage()

    # Create the GUI
    # Header should be at the top level, not inside a column
    with ui.header(elevated=True).style("background-color: #ffffff;").props(
        "fit=scale-down"
    ).classes("q-pa-xs-xs"):
        ui.image(ROOT + "assets/data/banner.png").style("height: 90px; width: 443px;")

    # Main content in a column
    with ui.column():
        with ui.row():
            # Left column: Upload and controls
            with ui.column():
                upload_element = (
                    ui.upload(
                        multiple=True,
                        on_upload=lambda e: handle_upload(e, refresh_file_view),
                        on_rejected=handle_reject,
                        label="Dateien auswählen",
                        auto_upload=True,
                        max_file_size=500_000_000,
                        max_files=5,
                    )
                    .props('accept="video/*, audio/*"')
                    .tooltip("Dateien auswählen")
                    .classes("w-full")
                    .style("width: 100%;")
                )
                upload_element.on(
                    "uploaded",
                    lambda e: handle_added(e, upload_element, refresh_file_view),
                )
                # Vocabulary section
                with ui.expansion("Vokabular", icon="menu_book").classes(
                    "w-full no-wrap"
                ).style("width: min(40vw, 400px)") as expansion:
                    ui.textarea(
                        label="Vokabular",
                        placeholder="Basel\nBasel Stadt\nBasilea",
                        on_change=lambda e: update_textarea_value(e.value),
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
                    "Anleitung öffnen",
                    on_click=lambda: ui.navigate.to(help, new_tab=True),
                ).props("no-caps")

            # Right column: File display
            display_files()


def update_textarea_value(value: str) -> None:
    """Update the vocabulary textarea value in storage"""
    app.storage.user["vocab"] = value


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        port=8080,
        title="Transcribo",
        storage_secret=STORAGE_SECRET,
        favicon=ROOT + "assets/data/logo.png",
        language="de-CH",
    )
