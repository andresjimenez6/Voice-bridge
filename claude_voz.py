#!/usr/bin/env python3
"""Superwhisper -> Claude -> voz.

Vigila los dictados de Superwhisper (vía su CLI), envía cada texto nuevo a Claude
y lee la respuesta en voz alta frase a frase mientras llega en streaming.

Uso:
    uv run claude_voz.py                      # responde a todos los dictados
    uv run claude_voz.py --activador claude   # solo a los que empiezan por "Claude..."
    uv run claude_voz.py --voz es-MX-JorgeNeural

Dicho en voz alta:
    "nueva conversación"  -> borra el historial
    (dictar mientras habla interrumpe la respuesta y atiende el nuevo mensaje)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

import anthropic
import edge_tts

MODELO = "claude-opus-5-5"

SYSTEM = (
    "Eres un asistente de voz. El usuario te habla por dictado (Superwhisper) y tu "
    "respuesta se convierte en voz con un sintetizador. Responde siempre en el idioma "
    "del usuario (normalmente español), de forma natural y conversacional, y breve: "
    "dos a cinco frases salvo que te pidan más detalle. No uses Markdown, listas, "
    "tablas, emojis, bloques de código ni URLs: todo se va a leer en voz alta. Escribe "
    "los números y símbolos como se pronuncian cuando haga falta. El texto dictado "
    "puede tener errores de transcripción; interpreta la intención más probable. "
    "La latencia importa: empieza tu respuesta visible de inmediato."
)

FRASE_FIN = re.compile(r"(?<=[.!?¡¿…:;])\s+|\n+")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def normaliza(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", sin_tildes.lower()).strip()


# ---------------------------------------------------------------- Superwhisper

def ultimo_dictado() -> tuple[int, str] | None:
    """Devuelve (createdAt, texto) del último dictado, o None."""
    try:
        out = subprocess.run(
            ["superwhisper", "read"], capture_output=True, text=True, timeout=10
        ).stdout
        data = json.loads(out)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or "createdAt" not in data:
        return None
    texto = (data.get("text") or data.get("inserted_text") or "").strip()
    return int(data["createdAt"]), texto


# ----------------------------------------------------------------------- Voz

class Voz:
    """Sintetiza frases con edge-tts y las reproduce en orden con mpv.

    Un hilo sintetiza (mientras otro reproduce la frase anterior) para que no haya
    silencios entre frases. `callar()` corta la reproducción y vacía las colas.
    """

    def __init__(self, voz: str, velocidad: str) -> None:
        self.voz = voz
        self.velocidad = velocidad
        self.frases: queue.Queue[tuple[int, str]] = queue.Queue()
        self.audios: queue.Queue[tuple[int, str]] = queue.Queue()
        self.turno = 0  # se incrementa al callar: descarta audio de turnos viejos
        self.reproductor: subprocess.Popen | None = None
        self.dir = tempfile.mkdtemp(prefix="claude_voz_")
        self.n = 0
        threading.Thread(target=self._sintetizador, daemon=True).start()
        threading.Thread(target=self._reproductor, daemon=True).start()

    def decir(self, frase: str) -> None:
        frase = frase.strip()
        if frase:
            self.frases.put((self.turno, frase))

    def callar(self) -> None:
        self.turno += 1
        for q in (self.frases, self.audios):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        if self.reproductor and self.reproductor.poll() is None:
            self.reproductor.terminate()

    def hablando(self) -> bool:
        return (
            not self.frases.empty()
            or not self.audios.empty()
            or (self.reproductor is not None and self.reproductor.poll() is None)
        )

    def _sintetizador(self) -> None:
        while True:
            turno, frase = self.frases.get()
            if turno != self.turno:
                continue
            self.n += 1
            ruta = os.path.join(self.dir, f"{self.n}.mp3")
            try:
                asyncio.run(
                    edge_tts.Communicate(frase, self.voz, rate=self.velocidad).save(ruta)
                )
            except Exception as e:  # red caída, voz inválida...
                log(f"Error de síntesis de voz: {e}")
                continue
            self.audios.put((turno, ruta))

    def _reproductor(self) -> None:
        while True:
            turno, ruta = self.audios.get()
            if turno == self.turno:
                self.reproductor = subprocess.Popen(
                    ["mpv", "--no-video", "--really-quiet", ruta],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.reproductor.wait()
            try:
                os.remove(ruta)
            except OSError:
                pass

    def cerrar(self) -> None:
        self.callar()
        shutil.rmtree(self.dir, ignore_errors=True)


# --------------------------------------------------------------------- Claude

class Conversacion:
    def __init__(self, voz: Voz, effort: str) -> None:
        self.cliente = anthropic.Anthropic()
        self.voz = voz
        self.effort = effort
        self.historial: list[dict] = []

    def reiniciar(self) -> None:
        self.historial.clear()

    def responder(self, texto: str, cancelado: threading.Event) -> None:
        mensajes = self.historial + [{"role": "user", "content": texto}]
        pendiente = ""
        turno = self.voz.turno
        print("Claude: ", end="", flush=True)
        try:
            with self.cliente.beta.messages.stream(
                model=MODELO,
                max_tokens=4096,
                system=SYSTEM,
                messages=mensajes,
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                for trozo in stream.text_stream:
                    if cancelado.is_set():
                        print(" [interrumpido]")
                        return  # no se guarda este turno en el historial
                    print(trozo, end="", flush=True)
                    pendiente += trozo
                    partes = FRASE_FIN.split(pendiente)
                    for frase in partes[:-1]:
                        if self.voz.turno == turno:
                            self.voz.decir(frase)
                    pendiente = partes[-1]
                final = stream.get_final_message()
        except anthropic.AuthenticationError:
            return
        except anthropic.APIStatusError as e:
            print()
            log(f"Error de la API ({e.status_code}): {e.message}")
            self.voz.decir("Ha habido un error con la API.")
            return
        except anthropic.APIConnectionError:
            print()
            log("Sin conexión con la API de Claude.")
            self.voz.decir("No puedo conectar con Claude.")
            return
        print()
        if cancelado.is_set():
            return

        if final.stop_reason == "refusal":
            self.voz.callar()
            self.voz.decir("Lo siento, no puedo responder a eso.")
            return  # una respuesta rechazada no se guarda en el historial
        if pendiente.strip():
            self.voz.decir(pendiente)
        # Se guarda el contenido completo (incluidos los bloques de razonamiento)
        # para que el historial siga siendo válido para la API.
        self.historial.extend(
            [
                {"role": "user", "content": texto},
                {"role": "assistant", "content": final.content},
            ]
        )


# ----------------------------------------------------------------------- main

def main() -> None:
    p = argparse.ArgumentParser(description="Habla con Claude usando Superwhisper.")
    p.add_argument("--voz", default="es-ES-AlvaroNeural",
                   help="voz de edge-tts (lista: uv run edge-tts --list-voices)")
    p.add_argument("--velocidad", default="+10%", help="velocidad de la voz, p. ej. +0%%, +20%%")
    p.add_argument("--activador", default="",
                   help="solo responder a dictados que empiecen por esta palabra (p. ej. 'claude')")
    p.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"],
                   help="esfuerzo de razonamiento de Claude (low = respuestas más rápidas)")
    p.add_argument("--intervalo", type=float, default=0.4, help="segundos entre comprobaciones")
    args = p.parse_args()

    if not shutil.which("superwhisper"):
        sys.exit("No encuentro el comando 'superwhisper' en el PATH.")
    if not shutil.which("mpv"):
        sys.exit("Necesito 'mpv' para reproducir el audio.")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("Falta ANTHROPIC_API_KEY. Exporta tu clave: export ANTHROPIC_API_KEY=sk-ant-...")

    activador = normaliza(args.activador)
    voz = Voz(args.voz, args.velocidad)
    conv = Conversacion(voz, args.effort)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    ultimo = ultimo_dictado()
    visto = ultimo[0] if ultimo else 0
    log(f"Escuchando dictados de Superwhisper (modelo {MODELO}, voz {args.voz}).")
    if activador:
        log(f"Solo respondo a dictados que empiecen por «{args.activador}».")
    log("Di «nueva conversación» para empezar de cero. Ctrl+C para salir.")

    hilo: threading.Thread | None = None
    cancelado = threading.Event()
    try:
        while True:
            time.sleep(args.intervalo)
            d = ultimo_dictado()
            if not d or d[0] <= visto:
                continue
            visto, texto = d
            if not texto:
                continue

            norm = normaliza(texto)
            if activador:
                if not norm.startswith(activador):
                    continue
                # quita la palabra de activación y la puntuación que la sigue
                texto = re.sub(rf"^\W*{re.escape(args.activador)}\W*", "", texto,
                               flags=re.IGNORECASE).strip() or texto
                norm = normaliza(texto)

            # Un dictado nuevo interrumpe la respuesta en curso.
            if (hilo and hilo.is_alive()) or voz.hablando():
                cancelado.set()
                voz.callar()
                if hilo:
                    hilo.join(timeout=5)

            print(f"\nTú: {texto}")
            if norm in ("nueva conversacion", "borra la conversacion", "reinicia la conversacion"):
                conv.reiniciar()
                voz.decir("Vale, empezamos una conversación nueva.")
                log("Historial borrado.")
                continue

            cancelado = threading.Event()
            hilo = threading.Thread(target=conv.responder, args=(texto, cancelado), daemon=True)
            hilo.start()
    except KeyboardInterrupt:
        print()
        log("Adiós.")
    finally:
        voz.cerrar()


if __name__ == "__main__":
    main()
