# Superwhisper → Claude → voz

Dictas con Superwhisper, el programa detecta el dictado
nuevo, se lo envía a Claude (`claude-opus-5-5`) y lee la respuesta en voz alta,
frase a frase mientras llega.

## Puesta en marcha

```bash
cd ~/superwhisper-claude
export ANTHROPIC_API_KEY=sk-ant-...     # https://console.anthropic.com/settings/keys
uv run claude_voz.py
```

## Opciones

| Opción | Por defecto | Qué hace |
|---|---|---|
| `--activador claude` | (vacío) | Solo responde a dictados que empiezan por «Claude…», para no contestar a lo que dictas en otras apps |
| `--voz` | `es-ES-AlvaroNeural` | Voz de edge-tts. Otras: `es-ES-ElviraNeural`, `es-MX-JorgeNeural`… (`uv run edge-tts --list-voices`) |
| `--velocidad` | `+10%` | Velocidad de la voz |
| `--effort` | `low` | Profundidad de razonamiento de Claude; `low` es lo más rápido |

## En uso

- Di **«nueva conversación»** para borrar el historial.
- Si dictas mientras Claude habla, se interrumpe y atiende lo nuevo.
- Superwhisper sigue pegando el texto en la ventana activa. Si no lo quieres:
  `superwhisper general set autoPasteEnabled false`.

## Notas

- La voz usa edge-tts (voces neuronales de Microsoft, online, sin clave) y `mpv`.
- Activado el respaldo de servidor `fallbacks: "default"`: si el modelo rechaza una
  petición, la API la reintenta en otro modelo automáticamente.
