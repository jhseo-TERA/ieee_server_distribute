"""PDFium's process-global runtime must be serialized across web features."""
import threading

PDFIUM_LOCK = threading.RLock()
