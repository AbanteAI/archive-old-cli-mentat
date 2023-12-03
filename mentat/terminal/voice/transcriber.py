import os
from typing import Iterable, Mapping

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from faster_whisper.transcribe import Segment
from prompt_toolkit.buffer import Buffer

from mentat.utils import mentat_dir_path

AUDIO_FILE = mentat_dir_path / "temp.wav"
CHUNK = 512
RATE = 16000
WHISPER_BATCH_SECS = 1
DELAY_TO_FREEZE = 1.5


def segments_to_transcript(segments: Iterable[Segment]):
    transcript = ""
    segments = [segment for segment in segments]

    if len(segments) == 0:
        return "", "", 0

    end = segments[-1].end
    fixed_transcript = ""
    freeze_start_time = 0
    for segment in segments:
        for word in segment.words:
            if word.start + DELAY_TO_FREEZE < end:
                freeze_start_time = word.end
                fixed_transcript += word.word
            transcript += word.word

    return transcript.strip(), fixed_transcript.strip(), freeze_start_time


class Transcriber:
    def __init__(self, buffer: Buffer) -> None:
        self.data: list[bytes] = []
        self.buffer = buffer
        self.start_text = buffer.text
        self.fixed = ""
        # Runs well on CPU. And works well for me if I speak clearly.
        # We should allow it to be user configurable.
        self.whisper_model_size = "tiny"
        # Necessary to suppress faster_whisper logging
        os.environ["CT2_VERBOSE"] = "-3"
        self.whisper_model = WhisperModel(self.whisper_model_size)
        # The transcript before this timestamp won't be changed
        self.frozen_timestamp = 0
        # How many frames are represented in the transcript
        self.processed_frames = 0
        self.whisper_semafore = False
        self.stream = sd.InputStream(
            samplerate=RATE,
            channels=1,
            dtype="float32",
            blocksize=CHUNK,
            callback=self.process_audio,
        )
        self.stream.start()

    def process_audio(
        self,
        in_data: np.ndarray,
        frames: int,
        time: Mapping[str, float],
        status: int,
    ):
        self.data.append(in_data.flatten())
        if not self.whisper_semafore:
            # During call_whisper the time gets out of sync. This check lets us catch up.
            if time.currentTime - time.inputBufferAdcTime < 0.3:
                if (
                    len(self.data) - self.processed_frames
                ) * frames / RATE > WHISPER_BATCH_SECS:
                    self.call_whisper()

    def call_whisper(self) -> None:
        self.whisper_semafore = True
        self.processed_frames = len(self.data)
        start = int(RATE * self.frozen_timestamp / CHUNK)
        send = np.concatenate(self.data[start:])

        segments, _ = self.whisper_model.transcribe(
            audio=send, beam_size=5, vad_filter=True, word_timestamps=True
        )
        transcript, fixed_transcript, end = segments_to_transcript(segments)
        self.frozen_timestamp += end

        self.buffer.text = self.start_text + self.fixed + " " + transcript

        self.fixed = (self.fixed + " " + fixed_transcript).strip()

        self.whisper_semafore = False

    def close(self):
        self.stream.stop()
        self.stream.close()
