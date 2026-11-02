"""Read-only photo library sources for the gallery.

A library source is an existing photo folder that Odysseus indexes *in place*.
Nothing under a source root is ever written, moved or deleted: index rows,
thumbnails, AI tags and embeddings all live in Odysseus's own storage
(``data/`` and the vector store). In Docker the folder is additionally mounted
read-only, so the kernel enforces the same guarantee.
"""
