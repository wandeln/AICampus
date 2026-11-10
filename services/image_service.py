"""
Bild-Generierung (Text→Bild) über den konfigurierten Image-Endpoint.

Getrennt von llm_service.py, weil es ein anderes Modell mit einem eigenen
Client ist (OpenAI-kompatible /v1/images/generations, z. B. vLLM-Omni +
Qwen-Image). Die Konfiguration (URL/Key/Modell) liefert get_effective_llm_config()
unter den Keys api_url_image / api_key_image / model_image (global, Admin).
"""

import base64
import logging
from time import monotonic
from typing import Optional

from httpx import AsyncClient
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
)

from services.llm_service import record_llm_debug_entry

logger = logging.getLogger("aicampus.image_service")

DEFAULT_IMAGE_SIZE = "1024x1024"
# Vom laufenden Endpoint geprüfte, akzeptierte Größen (Probe 11/2026).
ALLOWED_IMAGE_SIZES = {"512x512", "768x768", "1024x1024", "1024x768", "768x1024"}

# MIME → Dateierweiterung (für save_upload/replace_file).
_MIME_TO_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}

# Max. Kantenlänge des Ausgangsbilds für den Edit-Call. Größere Bilder werden
# runterskaliert, damit der (Base64-)Versand und das Modell nicht mit übermäßig
# großen Eingaben arbeiten.
MAX_EDIT_INPUT_PX = 2048


def downscale_for_edit(data: bytes, max_px: int = MAX_EDIT_INPUT_PX) -> tuple[bytes, Optional[str]]:
    """Skaliert ein Ausgangsbild für den Edit-Call: längste Kante <= max_px px.

    Kleine Bilder bleiben unverändert (mime=None). Transparenz wird als PNG,
    ansonsten als JPEG (q90) neu kodiert. Bei Decodier-Fehler unverändert.
    Returns: (bytes, mime) — mime=None ⇒ unverändert, alten mime behalten.
    """
    try:
        from io import BytesIO
        from PIL import Image

        im = Image.open(BytesIO(data))
        im.load()
        w, h = im.size
        longest = max(w, h)
        if longest <= max_px:
            return data, None
        scale = max_px / longest
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        if has_alpha:
            im = im.convert("RGBA")
            out = BytesIO()
            im.resize((nw, nh), Image.LANCZOS).save(out, "PNG")
            return out.getvalue(), "image/png"
        im = im.convert("RGB")
        out = BytesIO()
        im.resize((nw, nh), Image.LANCZOS).save(out, "JPEG", quality=90)
        return out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 — Decodier-Fehler ⇒ unverändert weiterspielen
        return data, None


async def generate_image(
    prompt: str,
    size: str = DEFAULT_IMAGE_SIZE,
    config: Optional[dict] = None,
    timeout: float = 180.0,
) -> dict:
    """Erzeugt ein Bild (Text→Bild) über den globalen Image-Endpoint.

    config: Aus get_effective_llm_config() — nutzt die Keys
    "api_url_image", "api_key_image", "model_image".

    Returns:
        {"success": True, "data": {"png_bytes": bytes, "mime": str,
         "revised_prompt": str, "model": str, "url": Optional[str],
         "latency_ms": int}}
        ODER {"success": False, "error": str}
    """
    cfg = config or {}
    api_url = cfg.get("api_url_image")
    api_key = cfg.get("api_key_image") or "sk-default"
    model = cfg.get("model_image")
    if not api_url or not model:
        return {"success": False, "error": "Image-Endpoint ist nicht konfiguriert (Admin → LLM-Einstellungen)."}
    if size not in ALLOWED_IMAGE_SIZES:
        size = DEFAULT_IMAGE_SIZE

    client = AsyncOpenAI(base_url=api_url, api_key=api_key, timeout=timeout)
    start = monotonic()
    png_bytes: Optional[bytes] = None
    revised_prompt = ""
    url: Optional[str] = None
    error: Optional[str] = None
    try:
        response = await client.images.generate(model=model, prompt=prompt, n=1, size=size)
        items = getattr(response, "data", None) or []
        item = items[0] if items else None
        b64 = getattr(item, "b64_json", None) if item else None
        url = getattr(item, "url", None) if item else None
        revised_prompt = (getattr(item, "revised_prompt", None) or "") if item else ""
        if b64:
            png_bytes = base64.b64decode(b64)
        elif url:
            # Fallback für Endpoints, die eine URL statt Base64 liefern.
            async with AsyncClient(timeout=60) as http:
                r = await http.get(url)
                r.raise_for_status()
                png_bytes = r.content
        if png_bytes is None:
            error = "Der Endpoint lieferte keine Bilddaten."
    except APIConnectionError as e:
        error = f"Keine Verbindung zum Image-Endpoint: {e}"
    except APITimeoutError:
        error = "Zeitüberschreitung bei der Bildgenerierung (>180 s)."
    except AuthenticationError as e:
        error = f"Authentifizierung fehlgeschlagen: {e}"
    except BadRequestError as e:
        error = f"Bildanfrage abgelehnt (u.U. falsches Modell oder Größe): {e}"
    except Exception as e:  # noqa: BLE001 — Fehlermeldung an die UI, kein Crash
        error = f"Fehler bei der Bildgenerierung: {e}"
    finally:
        await client.close()

    latency_ms = int((monotonic() - start) * 1000)
    success = png_bytes is not None
    record_llm_debug_entry(
        label="IMAGE_GENERATE",
        model=model,
        url=api_url,
        is_public=False,
        system_prompt="",
        prompt=prompt,
        response=f"Bild erzeugt ({size}, {len(png_bytes) if png_bytes else 0} Bytes)" if success else "(keine Bilddaten)",
        success=success,
        error=error or "",
        attempts=1,
    )
    if not success:
        return {"success": False, "error": error or "Unbekannter Fehler."}
    return {
        "success": True,
        "data": {
            "png_bytes": png_bytes,
            "mime": "image/png",
            "revised_prompt": revised_prompt,
            "model": model,
            "url": url,
            "latency_ms": latency_ms,
        },
    }


async def edit_image(
    prompt: str,
    base_image: tuple[bytes, str],
    size: str = DEFAULT_IMAGE_SIZE,
    config: Optional[dict] = None,
    timeout: float = 180.0,
) -> dict:
    """Erzeugt ein bearbeitetes Bild (Bild→Bild) über den Image-Endpoint
    (POST /v1/images/edits). `base_image` = (bytes, mime) des Ausgangsbilds;
    `prompt` beschreibt die gewünschte Änderung. Gleiche Rückgabe wie generate_image()."""
    cfg = config or {}
    api_url = cfg.get("api_url_image")
    api_key = cfg.get("api_key_image") or "sk-default"
    model = cfg.get("model_image")
    if not api_url or not model:
        return {"success": False, "error": "Image-Endpoint ist nicht konfiguriert (Admin → LLM-Einstellungen)."}
    if size not in ALLOWED_IMAGE_SIZES:
        size = DEFAULT_IMAGE_SIZE
    base_bytes, base_mime = base_image

    client = AsyncOpenAI(base_url=api_url, api_key=api_key, timeout=timeout)
    start = monotonic()
    png_bytes: Optional[bytes] = None
    revised_prompt = ""
    url: Optional[str] = None
    error: Optional[str] = None
    try:
        ext = _MIME_TO_EXT.get(base_mime, ".png")
        response = await client.images.edit(
            model=model,
            image=("base" + ext, base_bytes, base_mime),
            prompt=prompt,
            n=1,
            size=size,
        )
        items = getattr(response, "data", None) or []
        item = items[0] if items else None
        b64 = getattr(item, "b64_json", None) if item else None
        url = getattr(item, "url", None) if item else None
        revised_prompt = (getattr(item, "revised_prompt", None) or "") if item else ""
        if b64:
            png_bytes = base64.b64decode(b64)
        elif url:
            async with AsyncClient(timeout=60) as http:
                r = await http.get(url)
                r.raise_for_status()
                png_bytes = r.content
        if png_bytes is None:
            error = "Der Endpoint lieferte keine Bilddaten."
    except APIConnectionError as e:
        error = f"Keine Verbindung zum Image-Endpoint: {e}"
    except APITimeoutError:
        error = "Zeitüberschreitung bei der Bildbearbeitung (>180 s)."
    except AuthenticationError as e:
        error = f"Authentifizierung fehlgeschlagen: {e}"
    except BadRequestError as e:
        error = f"Bildbearbeitung abgelehnt (u.U. falsches Modell, Größe oder Ausgangsbild): {e}"
    except Exception as e:  # noqa: BLE001 — Fehlermeldung an die UI, kein Crash
        error = f"Fehler bei der Bildbearbeitung: {e}"
    finally:
        await client.close()

    latency_ms = int((monotonic() - start) * 1000)
    success = png_bytes is not None
    record_llm_debug_entry(
        label="IMAGE_EDIT",
        model=model,
        url=api_url,
        is_public=False,
        system_prompt="",
        prompt=prompt,
        response=f"Bild bearbeitet ({size}, {len(png_bytes) if png_bytes else 0} Bytes)" if success else "(keine Bilddaten)",
        success=success,
        error=error or "",
        attempts=1,
    )
    if not success:
        return {"success": False, "error": error or "Unbekannter Fehler."}
    return {
        "success": True,
        "data": {
            "png_bytes": png_bytes,
            "mime": "image/png",
            "revised_prompt": revised_prompt,
            "model": model,
            "url": url,
            "latency_ms": latency_ms,
        },
    }
