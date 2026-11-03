#!/usr/bin/env python3
"""Teste USB real usando os comandos normais do protocolo, sem gravar firmware.

O PING autoriza o firmware a consultar OBD quando houver CAN conectado.
Faça o primeiro teste somente com o ESP32 ligado por USB.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys
import threading
import time

INTEGRATION = Path(__file__).resolve().parents[2] / "integracao/events"
sys.path.insert(0, str(INTEGRATION))
from uart_reader import UartReader, REOPEN_S
from esp32_decoder import LINK_TIMEOUT_S

EXPECTED_FIRMWARE = "usb-sd-0.1.0"


class Samples:
    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()
        self.counts = Counter()

    def update(self, source, sample, ts=None):
        with self.lock:
            self.stream.write(json.dumps({"source": source, "ts": ts, "data": sample},
                                         ensure_ascii=False) + "\n")
            self.stream.flush()
            if not sample:
                self.counts[f"{source}_vazias"] += 1
            elif source == "imu" and all(key in sample for key in ("ax", "ay", "az")):
                self.counts["imu_amostras_validas"] += 1
                meta = sample.get("_meta", {})
                self.counts["imu_leituras_validas"] += meta.get("valid_readings", 0)
                self.counts["imu_leituras_totais"] += meta.get("total_readings", 0)
                if meta.get("simulated"):
                    self.counts["imu_amostras_simuladas"] += 1
            elif source == "obd" and sample.get("age_ms"):
                self.counts["obd_amostras_validas"] += 1

    def snapshot(self):
        with self.lock:
            return dict(self.counts)


class BenchReader(UartReader):
    """Reutiliza inclusive a fila de escritas parciais do leitor da Raspberry."""
    def __init__(self, manager, port, raw):
        super().__init__(manager, port)
        self.raw = raw
        self.received = 0
        self.errors = []
        self.fatal = False
        self.error_lock = threading.Lock()
        self.sync_baseline = None

    def pump(self, data):
        if data:
            self.raw.write(data)
            self.raw.flush()
            self.received += len(data)
        # Observe synchronization at frame boundaries: a corrupt frame arriving
        # after the first PONG in the same OS read must not enter the baseline.
        outgoing = bytearray()
        parts = data.split(b"\0")
        chunks = [part + b"\0" for part in parts[:-1]]
        if parts[-1] or not data:
            chunks.append(parts[-1])
        for chunk in chunks:
            outgoing.extend(super().pump(chunk))
            stats = self.decoder.stats()
            if stats["synced"]:
                with self.error_lock:
                    if self.sync_baseline is None:
                        self.sync_baseline = {key: stats["counters"][key]
                                              for key in ("corrupt", "lost")}
        return bytes(outgoing)

    def run(self):
        while not self._stop.is_set():
            try:
                self._read()
            except Exception as error:
                with self.error_lock:
                    self.errors.append(f"{type(error).__name__}: {error}")
                    self.fatal = not isinstance(error, OSError)
                if self.fatal:
                    return
            self._stop.wait(REOPEN_S)

    def diagnostics(self):
        with self.error_lock:
            return {"erros_de_conexao": list(self.errors), "erro_interno": self.fatal,
                    "contadores_na_primeira_sincronizacao": (
                        dict(self.sync_baseline) if self.sync_baseline is not None else None)}


def integrity(stats, baseline):
    counters = stats["counters"]
    return {
        "sincronizacao_observada": baseline is not None,
        "corrupt_total": counters["corrupt"],
        "lost_total": counters["lost"],
        "rejeitados_antes_da_sincronizacao": (
            baseline["corrupt"] if baseline is not None else counters["corrupt"]),
        "perdidos_antes_da_sincronizacao": (
            baseline["lost"] if baseline is not None else counters["lost"]),
        "rejeitados_apos_sincronizacao": (
            counters["corrupt"] - baseline["corrupt"] if baseline is not None else None),
        "perdidos_apos_sincronizacao": (
            counters["lost"] - baseline["lost"] if baseline is not None else None),
        "observacao": ("Os totais são preservados. Rejeições anteriores à primeira sincronização "
                       "são informadas separadamente; a causa não é determinada por este teste. "
                       "Após sincronizar, qualquer corrupção ou perda reprova a comunicação."),
    }


def verdict(stats, fatal=False, baseline=None):
    esp = stats.get("esp32") or {}
    sync = stats.get("sync", {})
    conditions = {
        "link_vigente": bool(stats.get("connected")),
        "protocolo_2": esp.get("protocol") == 2 and not stats.get("incompatible"),
        "plataforma_esp32": esp.get("platform") == "esp32",
        "dados_nao_sinteticos": esp.get("imu_simulated") is False,
        "firmware_esperado": esp.get("firmware") == EXPECTED_FIRMWARE,
        "ping_pong_sincronizado": bool(stats.get("synced") and sync.get("pairs", 0) > 0
                                      and sync.get("age_s") is not None
                                      and sync["age_s"] < LINK_TIMEOUT_S),
        "sem_erro_interno": not fatal,
        "sem_corrupcao_ou_perda_apos_sincronizacao": (
            baseline is not None
            and all(stats["counters"][key] == baseline[key] for key in ("corrupt", "lost"))),
    }
    return all(conditions.values()), conditions


def hardware_diagnostics(stats, counts):
    esp = stats.get("esp32") or {}
    sd_names = {0: "aguardando", 1: "gravando conforme STATUS", 2: "erro", 3: "cheio"}
    return {
        "adxl345_estado_informado": esp.get("imu_state", "sem STATUS"),
        "aceleracao_recebida": counts.get("imu_amostras_validas", 0) > 0,
        "sd_estado_informado": sd_names.get(esp.get("storage_state"), "sem STATUS"),
        "sd_leitura_do_cartao_validada": False,
        "can_obd_estado_informado": esp.get("obd_state", "sem STATUS"),
        "obd_recebido": counts.get("obd_amostras_validas", 0) > 0,
        "observacao": ("Este teste aprova somente a comunicação USB. A ausência de ADXL345, "
                       "cartão ou CAN não reprova esse link. STATUS de gravação não comprova "
                       "a integridade do conteúdo do cartão."),
    }


def run_check(port, seconds, output_dir):
    started = time.monotonic()
    interrupted = False
    with (output_dir / "usb.bin").open("xb") as raw, \
         (output_dir / "amostras.jsonl").open("x") as samples_file, \
         (output_dir / "status.jsonl").open("x") as status_file:
        samples = Samples(samples_file)
        reader = BenchReader(samples, port, raw)
        reader.start()
        next_report = started
        print(f"USB {port}, 460800 baud, durante {seconds:g} s. Enviando PING.", flush=True)
        try:
            while time.monotonic() - started < seconds:
                now = time.monotonic()
                if now >= next_report:
                    stats = reader.decoder.stats()
                    esp = stats.get("esp32") or {}
                    counts = samples.snapshot()
                    status_file.write(json.dumps({"decorrido_s": round(now - started, 3),
                                                 "decoder": stats, **reader.diagnostics()},
                                                ensure_ascii=False) + "\n")
                    status_file.flush()
                    print(f"{now - started:5.1f}s | bytes={reader.received} | "
                          f"link={'ativo' if stats['connected'] else 'aguardando'} | "
                          f"firmware={esp.get('firmware', 'sem STATUS')} | "
                          f"pares PING/PONG={stats['sync']['pairs']} | "
                          f"aceleração={counts.get('imu_amostras_validas', 0)} | "
                          f"OBD={counts.get('obd_amostras_validas', 0)}", flush=True)
                    next_report = now + 1
                if reader.diagnostics()["erro_interno"]:
                    break
                time.sleep(min(0.05, max(0, seconds - (time.monotonic() - started))))
        except KeyboardInterrupt:
            interrupted = True
        finally:
            reader.stop()
        stats = reader.decoder.stats()
        diagnostics = reader.diagnostics()
        counts = samples.snapshot()
        baseline = diagnostics["contadores_na_primeira_sincronizacao"]
        link_integrity = integrity(stats, baseline)
        approved, conditions = verdict(stats, diagnostics["erro_interno"], baseline)
        approved = approved and not interrupted
        summary = {
            "resultado": "APROVADO" if approved else "NAO_APROVADO",
            "porta": port, "baud": 460800, "firmware_esperado": EXPECTED_FIRMWARE,
            "duracao_s": round(time.monotonic() - started, 3), "interrompido": interrupted,
            "bytes_recebidos": reader.received, "criterios": conditions,
            "amostras": counts, "decoder": stats, **diagnostics,
            "integridade": link_integrity,
            "componentes": hardware_diagnostics(stats, counts),
        }
        (output_dir / "resumo.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(f"\nUSB: {summary['resultado']}", flush=True)
        print(f"Quadros rejeitados: total={link_integrity['corrupt_total']}, "
              f"antes de sincronizar={link_integrity['rejeitados_antes_da_sincronizacao']}, "
              f"após sincronizar={link_integrity['rejeitados_apos_sincronizacao']}; "
              f"perdas após sincronizar={link_integrity['perdidos_apos_sincronizacao']}", flush=True)
        if link_integrity["rejeitados_antes_da_sincronizacao"]:
            print("Há bytes rejeitados antes da sincronização; a causa não foi determinada.", flush=True)
        if not approved:
            print("Critérios pendentes: " + ", ".join(key for key, ok in conditions.items() if not ok), flush=True)
        if diagnostics["erros_de_conexao"]:
            print("Último erro: " + diagnostics["erros_de_conexao"][-1], flush=True)
        print("ADXL345: " + summary["componentes"]["adxl345_estado_informado"] +
              " | SD: " + summary["componentes"]["sd_estado_informado"] +
              " | CAN/OBD: " + summary["componentes"]["can_obd_estado_informado"], flush=True)
        print(f"Relatório: {output_dir / 'resumo.json'}", flush=True)
        return 0 if approved else 1


def duration(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("duração deve ser um número de segundos")
    if not math.isfinite(number) or not 1 <= number <= 120:
        raise argparse.ArgumentTypeError("duração deve estar entre 1 e 120 segundos")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--seconds", type=duration, default=20)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.output_dir.is_dir():
        parser.error("--output-dir deve indicar um diretório existente e sem arquivos desta execução")
    try:
        return run_check(args.port, args.seconds, args.output_dir.resolve())
    except OSError as error:
        print(f"Não foi possível executar o teste USB: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
