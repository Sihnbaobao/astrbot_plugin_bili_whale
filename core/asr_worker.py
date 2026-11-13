"""把一段 16k 单声道 wav 转成文字。

单独起进程跑：模型加载要几百 MB，用完就退，别常驻在 AstrBot 进程里。
用法：python asr_worker.py <模型目录> <wav 路径>
"""

from __future__ import annotations

import sys
import wave

import numpy as np
import sherpa_onnx


def main() -> None:
    """读 wav，转写，把文字打到 stdout。"""
    model_dir, wav_path = sys.argv[1], sys.argv[2]
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=f"{model_dir}/model.int8.onnx",
        tokens=f"{model_dir}/tokens.txt",
        num_threads=2,
        use_itn=True,
        language="zh",
        debug=False,
    )
    with wave.open(wav_path, "rb") as handle:
        if handle.getframerate() != 16000:
            print("采样率不对", file=sys.stderr)
            sys.exit(2)
        frames = handle.readframes(handle.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, audio)
    recognizer.decode_stream(stream)
    print(stream.result.text.strip())


if __name__ == "__main__":
    main()
