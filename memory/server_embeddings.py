"""Embedding model used by the LangGraph Server memory index."""

from config import config
from memory.long_term import build_memory_embeddings


memory_embeddings = build_memory_embeddings(config)
