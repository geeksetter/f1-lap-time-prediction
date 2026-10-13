"""Execute the diagnostic notebook using this interpreter and a private local kernel."""

import json
import os
import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient

root = Path(__file__).resolve().parents[1]
spec = root / ".local/jupyter/kernels/f1"
spec.mkdir(parents=True, exist_ok=True)
(spec / "kernel.json").write_text(
    json.dumps(
        {
            "argv": [
                sys.executable,
                "-m",
                "ipykernel_launcher",
                "-f",
                "{connection_file}",
            ],
            "display_name": "F1 project",
            "language": "python",
        }
    )
)
os.environ["JUPYTER_PATH"] = str(root / ".local/jupyter")
os.environ["JUPYTER_RUNTIME_DIR"] = str(root / ".local/jupyter/runtime")
Path(os.environ["JUPYTER_RUNTIME_DIR"]).mkdir(parents=True, exist_ok=True)
os.environ["IPYTHONDIR"] = str(root / ".local/ipython")
path = root / "notebooks/01_data_quality.ipynb"
nb = nbformat.read(path, as_version=4)
NotebookClient(
    nb, timeout=120, kernel_name="f1", resources={"metadata": {"path": str(root)}}
).execute()
nb.metadata["kernelspec"] = {
    "display_name": "Python 3",
    "language": "python",
    "name": "python3",
}
nbformat.validate(nb)
nbformat.write(nb, path)
print("All notebook cells executed against PostgreSQL")
