"""Pre-download embedding model at Docker build time."""
import os
import sys

model_name = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
print(f"Pre-downloading: {model_name}")

try:
    from sentence_transformers import SentenceTransformer
    m = SentenceTransformer(model_name, trust_remote_code=True)
    dim = m.get_sentence_embedding_dimension()
    print(f"Downloaded via sentence-transformers: dim={dim}")
except Exception as e:
    print(f"sentence-transformers failed ({e}), trying transformers...")
    from transformers import AutoModel, AutoTokenizer
    import torch
    m = AutoModel.from_pretrained(
        model_name,
        trust_remote_code=True,
        torch_dtype=torch.float16,
    )
    try:
        AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    except Exception:
        pass
    print(f"Downloaded via transformers: {type(m).__name__}")
