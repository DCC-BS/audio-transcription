from dataclasses import dataclass
from dataclasses_json import dataclass_json


@dataclass_json
@dataclass
class FileStatus:
    filename: str
    out_dir: str
    status_message: str
    progress_percentage: float  # 0.0 to 100.0
    estimated_time_remaining: int  # seconds
    last_modified: float  # timestamp
    queue_position: int = 0
    
    @classmethod
    def create_queued(cls, filename: str, out_dir: str, last_modified: float, queue_position: int, estimated_time: int = 0) -> 'FileStatus':
        return cls(
            filename=filename,
            out_dir=out_dir,
            status_message=f"In Warteschlange (Position {queue_position}). Geschätzte Wartezeit: {estimated_time} Sekunden",
            progress_percentage=0.0,
            estimated_time_remaining=estimated_time,
            last_modified=last_modified,
            queue_position=queue_position
        )
    
    @classmethod
    def create_completed(cls, filename: str, out_dir: str, last_modified: float) -> 'FileStatus':
        return cls(
            filename=filename,
            out_dir=out_dir,
            status_message="Datei transkribiert",
            progress_percentage=100.0,
            estimated_time_remaining=0,
            last_modified=last_modified
        )
    
    @classmethod
    def create_error(cls, filename: str, out_dir: str, last_modified: float, error_message: str) -> 'FileStatus':
        return cls(
            filename=filename,
            out_dir=out_dir,
            status_message=error_message,
            progress_percentage=-1.0,
            estimated_time_remaining=0,
            last_modified=last_modified
        )
    