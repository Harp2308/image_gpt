import uvicorn
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    bat_path = Path(__file__).parent / "start_workers.bat"
    
    worker_process = subprocess.Popen(
        [str(bat_path)],
        shell=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    
    try:
        uvicorn.run(
            "colep_ai.api.ingestion_indexing_main:app",
            host="0.0.0.0",
            port=4001,
            reload=True,
        )
    finally:
        worker_process.terminate()
        worker_process.wait()