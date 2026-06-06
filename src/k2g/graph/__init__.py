"""K2G graph package — community detection engine (leidenalg / igraph).

The v1 networkx subgraph / path / centrality / view modules (BP-87) were removed
when the link-form Manager UI replaced the Streamlit viewer, and networkx was
dropped (it hangs on float-jaccard weights; leidenalg is sub-second). The
remaining engine is ``leiden_community`` (BP-90) — import it directly:

    from k2g.graph import leiden_community
"""
