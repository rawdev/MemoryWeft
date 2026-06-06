"""K2G Adapters: vector search results → downstream format conversion.

Direct imports:
    from k2g.adapters.vector_to_prompt import VectorToPromptAdapter
    from k2g.adapters.vector_to_source import VectorToSourceAdapter, SourceRef
"""

__all__ = [
    "VectorToPromptAdapter",
    "VectorToSourceAdapter",
    "VectorToTextAdapter",
    "EntityAligner",
    "SourceRef",
    "GeneratedScene",
]
