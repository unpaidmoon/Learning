"""No network, shell, decoding of frames, or model tools in this worker."""

import heapq
import json
import os
import sys
from pathlib import Path


def main():
    runtime, output = Path(sys.argv[1]), Path(sys.argv[2])
    duration_ms = int(sys.argv[3])
    sys.path.insert(0, str(runtime))
    import av

    inputs = [av.open(value) for value in sys.argv[4:]]
    temporary = output.with_suffix(".partial.mp4")
    clocks = {}
    streams = {}
    target = av.open(str(temporary), "w", format="mp4", options={"movflags": "+faststart"})
    try:
        for container in inputs:
            for stream in container.streams:
                if stream.type in {"video", "audio"}:
                    if stream.type in clocks:
                        raise RuntimeError("DUPLICATE_TRACK")
                    streams[(id(container), stream.index)] = target.add_stream_from_template(stream)
                    clocks[stream.type] = [None, None, 0]

        def packets(container):
            for packet in container.demux():
                if packet.dts is not None and (id(container), packet.stream.index) in streams:
                    yield (float(packet.dts * packet.time_base), id(container), packet)

        for _, owner, packet in heapq.merge(
            *(packets(container) for container in inputs), key=lambda value: value[:2]
        ):
            clock = clocks[packet.stream.type]
            start = (
                float(packet.pts * packet.time_base)
                if packet.pts is not None
                else float(packet.dts * packet.time_base)
            )
            end = start + float((packet.duration or 0) * packet.time_base)
            clock[0] = min(start, clock[0]) if clock[0] is not None else start
            clock[1] = max(end, clock[1]) if clock[1] is not None else end
            clock[2] += 1
            packet.stream = streams[(owner, packet.stream.index)]
            target.mux(packet)
    finally:
        target.close()
        for container in inputs:
            container.close()
    expected = {"audio"} if len(clocks) == 1 and "audio" in clocks else {"audio", "video"}
    if set(clocks) != expected or any(
        v[0] is None or v[2] < 1 or abs(v[0]) > 1 or v[1] * 1000 + 2000 < duration_ms
        for v in clocks.values()
    ):
        raise RuntimeError("MEDIA_DURATION_INCOMPLETE")
    with av.open(str(temporary)) as saved:
        if {s.type for s in saved.streams} != expected:
            raise RuntimeError("MEDIA_TRACK_MISSING")
        if not saved.duration or saved.duration / 1000 + 2000 < duration_ms:
            raise RuntimeError("MEDIA_DURATION_INCOMPLETE")
    with temporary.open("r+b") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, output)
    print(
        json.dumps(
            {
                "tracks": clocks,
                "duration_ms": duration_ms,
                "verified": "packet-timeline-and-container",
            }
        )
    )


if __name__ == "__main__":
    main()
