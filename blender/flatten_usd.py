from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pxr import Usd


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--validation", required=True)
    args = parser.parse_args(argv)

    source = Path(args.input).resolve()
    output = Path(args.output).resolve()
    validation = Path(args.validation).resolve()
    stage = Usd.Stage.Open(str(source))
    if not stage:
        raise RuntimeError(f"Не удалось открыть USD Stage: {source}")
    flattened = stage.Flatten()
    default_prim = stage.GetDefaultPrim()
    if default_prim:
        flattened.defaultPrim = default_prim.GetPath().pathString
    flattened.timeCodesPerSecond = stage.GetTimeCodesPerSecond()
    flattened.startTimeCode = stage.GetStartTimeCode()
    flattened.endTimeCode = stage.GetEndTimeCode()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not flattened.Export(str(output)):
        raise RuntimeError(f"Не удалось экспортировать flat-USD: {output}")

    verified = Usd.Stage.Open(str(output))
    if not verified:
        raise RuntimeError(f"Не удалось повторно открыть flat-USD: {output}")
    animated_prims = []
    animated_attributes = 0
    for prim in verified.Traverse():
        attributes = [attribute for attribute in prim.GetAttributes() if attribute.GetNumTimeSamples() > 0]
        if attributes:
            animated_prims.append(prim.GetPath().pathString)
            animated_attributes += len(attributes)
    if not animated_prims:
        raise RuntimeError("Flat-USD не содержит timeSamples")
    result = {
        "source": source.name,
        "flattened": output.name,
        "prim_count": sum(1 for _ in verified.Traverse()),
        "animated_prim_count": len(animated_prims),
        "animated_attribute_count": animated_attributes,
        "time_codes_per_second": verified.GetTimeCodesPerSecond(),
        "start_time_code": verified.GetStartTimeCode(),
        "end_time_code": verified.GetEndTimeCode(),
        "animated_prims": animated_prims,
    }
    validation.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
