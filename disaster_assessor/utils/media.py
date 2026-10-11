"""Bounded video sampling with temporary-file cleanup, no remote media service."""

import math
import os
import tempfile
import cv2
from PIL import Image


def sample_video_frames(data, suffix=".mp4", interval_seconds=2.0, max_frames=12):
    if interval_seconds <= 0 or not 1 <= max_frames <= 60:
        raise ValueError("Invalid sampling limits")
    path = None
    capture = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as file:
            path = file.name
            file.write(data)
        capture = cv2.VideoCapture(path)
        if not capture.isOpened():
            raise ValueError("Video codec could not be opened. Try an H.264 MP4.")
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError(
                "Video frame rate is unavailable. Export still frames instead."
            )
        for sample in range(max_frames):
            frame_index = round(sample * interval_seconds * fps)
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok:
                if sample == 0:
                    raise ValueError("The video has no decodable frames.")
                break
            yield frame_index / fps, Image.fromarray(
                cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            )
    finally:
        if capture is not None:
            capture.release()
        if path is not None:
            os.unlink(path)
