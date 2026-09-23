"""Medidor de tokens: velocidad en vivo y consumo acumulado de la jornada (desde que se abrió el programa).

Lógica pura, sin ventana, para poder probarla con un reloj falso. Los tokens EXACTOS llegan con el evento «usage» de
cada paso; mientras el texto se está generando no hay cifra exacta, así que la velocidad en vivo se estima con
caracteres / CHARS_PER_TOKEN y se marca con «~». Al terminar el paso, la velocidad pasa a ser la exacta
(tokens de salida informados / tiempo de generación).
"""
import time
from collections import deque

CHARS_PER_TOKEN = 3.5      # aproximación para texto mezclado (español, código); solo se usa en vivo
WINDOW = 3.0               # segundos de historia para la velocidad en vivo
STALE = 2.0                # sin texto nuevo durante tanto tiempo => ya no se considera «generando»
MIN_SPAN = 0.4             # con menos tiempo que esto entre el primer y el último trozo no hay velocidad confiable


class TokenMeter:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self):
        self.acc_in = 0            # tokens de entrada acumulados (exactos)
        self.acc_out = 0           # tokens de salida acumulados (exactos, más estimados de pasos cortados)
        self.est_steps = 0         # cuántos pasos se sumaron por estimación (sin «usage»)
        self.last_rate = None      # última velocidad exacta medida (tokens/s)
        self._step_reset()

    def _step_reset(self):
        self._chars = 0
        self._pieces = deque()     # (instante, caracteres)
        self._t_first = None
        self._t_last = None

    # ------------------------------------------------------------ eventos

    def step_begin(self):
        self._step_reset()

    def piece(self, text):
        if not text:
            return
        now = self.clock()
        if self._t_first is None:
            self._t_first = now
        self._t_last = now
        self._chars += len(text)
        self._pieces.append((now, len(text)))
        while self._pieces and now - self._pieces[0][0] > WINDOW:
            self._pieces.popleft()

    def usage(self, u):
        """Llegan los números exactos del paso: reemplazan a la estimación."""
        out = int(u.get("completion_tokens", 0) or 0)
        self.acc_in += int(u.get("prompt_tokens", 0) or 0)
        self.acc_out += out
        if self._t_first is not None and self._t_last - self._t_first >= MIN_SPAN and out > 0:
            # el primer trozo marca el inicio del reloj; sus tokens no cuentan en la cuenta de velocidad
            self.last_rate = out / (self._t_last - self._t_first)
        self._step_reset()

    def step_end(self):
        """Si el paso terminó sin «usage» (cancelado, error de red), lo generado se suma como estimación."""
        if self._chars:
            self.acc_out += self._est_tokens(self._chars)
            self.est_steps += 1
        self._step_reset()

    # ------------------------------------------------------------ lectura

    @staticmethod
    def _est_tokens(chars):
        return int(round(chars / CHARS_PER_TOKEN))

    def generating(self):
        return self._t_last is not None and self.clock() - self._t_last <= STALE

    def rate(self):
        """(tokens por segundo, en_vivo). None si todavía no hay dato."""
        now = self.clock()
        if self.generating():
            live = [(t, c) for t, c in self._pieces if now - t <= WINDOW]
            if len(live) >= 2:
                span = live[-1][0] - live[0][0]
                if span >= MIN_SPAN:
                    # los caracteres del primer trozo llegaron ANTES de que empezara a correr el reloj
                    return self._est_tokens(sum(c for _, c in live[1:])) / span, True
        if self.last_rate is not None:
            return self.last_rate, False
        return None

    def total(self):
        """Tokens de la jornada: acumulado exacto + lo estimado del paso en curso."""
        return self.acc_in + self.acc_out + self._est_tokens(self._chars)

    def estimated(self):
        return self.est_steps > 0 or self._chars > 0
