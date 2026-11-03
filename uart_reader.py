"""
V-GUARD — Leitor da UART do ESP32.

Lê os bytes do ESP32, passa ao Decoder e entrega as amostras de IMU e
OBD ao EventManager:

    ESP32 --UART--> UartReader --feed()--> Decoder --amostras--> manager.update()
          <-------- PING e TAXAS <-- data_to_send()

O esp32_decoder.py acompanha OBD-II/tools na versão USB/ADXL345.
Também aceita o protocolo 1 do firmware_telemetria.

Roda em uma thread do mesmo processo do EventManager:

    reader = UartReader(manager)
    reader.start()
    reader.decoder.set_rates(imu=50)     # RF29, de qualquer thread
"""

import errno
import glob
import logging
import os
import select
import termios
import threading
import tty

from esp32_decoder import Decoder


# ============================================================
# CONFIGURAÇÃO
# ============================================================

# UART da Pi ligada ao ESP32 (a confirmar com o Thiago). No notebook,
# com o ESP32 falso do firmware_telemetria: /tmp/vguard-esp32.
PORT = os.environ.get("VGUARD_SERIAL") or os.environ.get("VGUARD_UART")


def resolve_port(path=None):
    """Never silently bind an arbitrary USB device when more than one is present."""
    if path:
        return path
    candidates = sorted(glob.glob("/dev/serial/by-id/*"))
    if len(candidates) == 1:
        return candidates[0]
    raise OSError(errno.ENODEV, "defina VGUARD_SERIAL=/dev/serial/by-id/...; "
                  f"encontradas {len(candidates)} portas estáveis")

# O Decoder precisa de um feed() a cada 100 ms no máximo, mesmo sem
# bytes: é nele que saem o PING e a percepção de ESP32 mudo.
POLL_S = 0.1

# Espera antes de reabrir a porta depois de um erro.
REOPEN_S = 2.0

# Valor do OBD mais velho que isto é descartado. Os seis PIDs do RF05
# são lidos 1 vez/s, então idades de até ~1 s são normais. Carro
# desligado já chega como {} pelo Decoder; isto pega um PID isolado que
# parou de responder. Valor INICIAL, a calibrar no carro.
OBD_MAX_AGE_MS = 3000


def open_port(path, baud=termios.B460800):
    """
    Abre a UART em 8N1, sem controle de fluxo. O padrão de 460800 baud é
    o do ESP32 (PROTOCOLO.md); o GPS passa a dele.
    """
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[2] &= ~(termios.CSTOPB | termios.CRTSCTS | termios.HUPCL)
        attrs[4] = attrs[5] = baud
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return fd
    except BaseException:
        os.close(fd)
        raise



def drop_stale(sample, max_age_ms=OBD_MAX_AGE_MS):
    """
    Tira da amostra OBD os valores com age_ms acima do limite. Se nenhum
    sobrar, devolve {}, que o update() trata como OBD parado.
    """
    if sample.get("_meta", {}).get("protocol") == 2:
        max_age_ms = 12000
    ages = sample.get("age_ms", {})
    fresh = {key: age for key, age in ages.items() if age <= max_age_ms}

    if len(fresh) == len(ages):
        return sample
    if not fresh:
        return {}
    return {**{key: value for key, value in sample.items() if key not in ages},
            **{key: sample[key] for key in fresh}, "age_ms": fresh}


class UartReader:

    def __init__(self, manager, port=PORT, baud=termios.B460800):
        self.manager = manager
        self.port = port
        self.baud = baud
        self.decoder = Decoder()
        self._stop = threading.Event()
        self._thread = None

    def pump(self, data):
        """
        Um passo: bytes lidos (pode ser b"") viram update(). Retorna os
        bytes a escrever na porta, que devem sair na hora (DECODIFICADOR.md §2.2).
        """
        for source, ts, sample in self.decoder.feed(data):
            if source == "obd":
                sample = drop_stale(sample)
            self.manager.update(source, sample, ts=ts)

        return self.decoder.data_to_send()

    def run(self):
        # Se o leitor morrer, a viagem inteira fica sem IMU e OBD: qualquer
        # erro é registrado e a porta é reaberta.
        while not self._stop.is_set():
            try:
                self._read()
            except OSError as error:
                # porta ausente ou cabo solto: esperado, sem traceback
                logging.warning(
                    "UART %s: %s; nova tentativa em %g s",
                    self.port, error, REOPEN_S
                )
            except Exception:
                logging.exception(
                    "leitor da UART %s falhou; nova tentativa em %g s",
                    self.port, REOPEN_S
                )
            self._stop.wait(REOPEN_S)

    def _read(self):
        fd = open_port(resolve_port(self.port), self.baud)
        try:
            for source, ts, sample in self.decoder.transport_reset():
                self.manager.update(source, sample, ts=ts)
            pending = b""
            while not self._stop.is_set():
                ready, writable, _ = select.select([fd], [fd] if pending else [], [], POLL_S)
                try:
                    data = os.read(fd, 4096) if ready else b""
                except BlockingIOError:
                    data = b""
                    ready = []
                if ready and not data:
                    raise OSError("porta fechada")
                pending += self.pump(data)
                if len(pending) > 4096:
                    raise OSError("fila de comandos serial saturada")
                if pending and (writable or not ready):
                    try:
                        written = os.write(fd, pending)
                    except BlockingIOError:
                        continue
                    if written == 0:
                        raise OSError("escrita serial sem progresso")
                    pending = pending[written:]
        finally:
            os.close(fd)

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
