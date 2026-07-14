"""
One-time download of the reranker model snapshot.

Run this once — locally, or as a build step in your Docker image / CI
pipeline — so the reranker never has to reach the Hugging Face Hub at
request time. Same reasoning as pinning a specific Azure OpenAI deployment
in embedder.py: you want a fixed, known-good model on disk, not "whatever
the Hub serves right now."

Usage:
    python download_reranker_model.py
"""

from huggingface_hub import snapshot_download

REPO_ID = "BAAI/bge-reranker-v2-m3"

# TODO before production: pin this to a specific commit hash rather than
# "main", e.g. REVISION = "abc123...". Find it with:
#   huggingface_hub.list_repo_refs(REPO_ID)
# Pinning means an upstream model update can never silently change your
# retrieval behavior between one deploy and the next.
REVISION = "main"

LOCAL_DIR = "./models/bge-reranker-v2-m3"

if __name__ == "__main__":
    path = snapshot_download(
        repo_id=REPO_ID,
        revision=REVISION,
        local_dir=LOCAL_DIR,
    )
    print(f"Downloaded '{REPO_ID}' @ {REVISION} to: {path}")
    print("Set RERANKER_MODEL_PATH to this path (or leave default if unchanged) "
          "and pin REVISION above before shipping.")
