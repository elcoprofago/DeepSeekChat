"""Medidor de tokens con reloj falso. Uso: python test_meter.py"""
import sys

import meter

fallas = []


def check(nombre, cond, detalle=""):
    print(("OK    " if cond else "FALLA ") + nombre + (f"  [{str(detalle)[:300]}]" if detalle and not cond else ""))
    if not cond:
        fallas.append(nombre)


class Reloj:
    t = 100.0

    def __call__(self):
        return self.t


r = Reloj()
m = meter.TokenMeter(clock=r)
check("recién creado: sin velocidad y total 0", m.rate() is None and m.total() == 0 and not m.generating())

# --- velocidad en vivo: 35 caracteres cada 0,5 s = 10 tokens estimados cada 0,5 s = 20 tok/s
m.step_begin()
for _ in range(6):
    m.piece("x" * 35)
    r.t += 0.5
r.t -= 0.5
v = m.rate()
check("en vivo: ~20 tok/s y marcada como estimada", v is not None and v[1] is True and abs(v[0] - 20) < 1.0, v)
check("generando mientras llega texto", m.generating())
check("el total incluye lo estimado del paso en curso", m.total() == round(6 * 35 / meter.CHARS_PER_TOKEN) and m.estimated(), m.total())

# --- llegan los números exactos: reemplazan a la estimación
m.usage({"prompt_tokens": 1000, "completion_tokens": 50})
check("con usage: el acumulado usa los números exactos, sin duplicar la estimación", m.acc_in == 1000 and m.acc_out == 50 and m.total() == 1050, (m.acc_in, m.acc_out, m.total()))
v = m.rate()
check("con usage: la velocidad pasa a exacta (50 tok en 2,5 s = 20 tok/s), no en vivo",
      v is not None and v[1] is False and abs(v[0] - 20.0) < 0.01, v)
check("tras el usage no queda estimación pendiente", not m.estimated())

# --- sin texto nuevo, deja de estar «generando» y se conserva la última velocidad exacta
m.step_begin()
m.piece("hola")
r.t += meter.STALE + 1
check("sin texto nuevo por un rato: ya no está generando y muestra la última velocidad exacta", not m.generating() and m.rate() == (m.last_rate, False), m.rate())
m.step_end()
check("paso cortado sin usage: se suma como estimación y queda marcado", m.acc_out == 50 + round(4 / meter.CHARS_PER_TOKEN) and m.est_steps == 1, (m.acc_out, m.est_steps))
check("y sigue habiendo estimación en el acumulado", m.estimated())

# --- acumula entre pasos
m.step_begin()
m.piece("a" * 70)
r.t += 1.0
m.piece("a" * 70)
m.usage({"prompt_tokens": 2000, "completion_tokens": 40, "completion_tokens_details": {"reasoning_tokens": 10}})
check("dos pasos: entrada y salida se suman", m.acc_in == 3000 and m.acc_out >= 90, (m.acc_in, m.acc_out))

# --- casos borde
m.reset()
check("reset borra todo", m.total() == 0 and m.rate() is None and not m.estimated())
m.step_begin()
m.piece("")
check("trozo vacío no cuenta", m.total() == 0 and not m.generating())
m.piece("a")
m.usage({"prompt_tokens": 10, "completion_tokens": 5})
check("un solo trozo: no inventa velocidad (span 0)", m.rate() is None and m.total() == 15, (m.rate(), m.total()))
m.usage({})
check("usage vacío no rompe", m.total() == 15)
m.usage({"prompt_tokens": None, "completion_tokens": None})
check("usage con None no rompe", m.total() == 15)

print("\nFALLAS:", fallas if fallas else "ninguna")
sys.exit(1 if fallas else 0)
