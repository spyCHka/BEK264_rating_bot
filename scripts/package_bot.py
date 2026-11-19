"""Build a self-contained zipapp from the Python modules at the project root."""
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    with ZipFile(output, "w") as bundle:
        for source in sorted(ROOT.glob("*.py")):
            name = "__main__.py" if source.name == "main.py" else source.name
            code = source.read_bytes()
            compile(code, source.name, "exec")
            bundle.writestr(ZipInfo(name), code, compress_type=ZIP_DEFLATED)


if __name__ == "__main__":
    build(sys.argv[1])
