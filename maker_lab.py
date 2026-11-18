"""Energia que dá Lucro: monitor físico ou demonstração explicitamente simulada."""
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from modulos.energia import BRASILIA, ConfigEnergia, MonitorEnergia, RegistroEnergia, executar_monitoramento


class IADemonstracao:
    """Dublê explícito; nenhuma chamada de rede."""
    falhar = False

    def analisar_iluminacao(self, contexto, timeout_s=10):
        if self.falhar:
            raise TimeoutError("falha de IA simulada")
        return {"classificacao": "possivel_desperdicio",
                "justificativa": "SIMULAÇÃO: claridade persistente fora do expediente.",
                "recomendacao": "Verifique a origem da claridade e a necessidade da iluminação.",
                "mensagem_falada": "Há claridade persistente no estoque fora do horário. Verifique se a iluminação é necessária."}


def demonstrar(pasta=None):
    print("SIMULAÇÃO EXPLÍCITA: Arduino, relógio e IA dublês. Sem rede, áudio ou motor.")
    print("Referências ADC sintéticas 100/900. Sem potência/tarifa: custo desconhecido, exportação pendente.")
    tempo = [0.]
    base = datetime(2026, 11, 12, 22, tzinfo=BRASILIA)
    ia = IADemonstracao()
    cfg = ConfigEnergia(referencia_escuro=100, referencia_claro=900, persistencia_s=3,
                        pausa_alerta_s=5, intervalo_ia_s=1)
    registro = RegistroEnergia(pasta or Path(__file__).resolve().parent / "dados/maker/simulacao_v4")
    monitor = MonitorEnergia(ia, cfg, registro, simulado=True, relogio=lambda: tempo[0],
                             agora=lambda: base + timedelta(seconds=tempo[0]))
    cenarios = [
        ("Ambiente escuro", 100, 3, False, False),
        ("Iluminado durante funcionamento", 900, 4, True, False),
        ("Oscilação breve fora do horário", 900, 2, False, False),
        ("Fim da oscilação", 100, 2, False, False),
        ("Iluminação persistente fora do horário", 900, 6, False, False),
        ("Resolução da ocorrência", 100, 2, False, False),
        ("Falha da IA com nova ocorrência", 900, 6, False, True),
        ("Falha de comunicação", None, 5, False, False),
        ("Comunicação recuperada, ambiente escuro", 100, 2, False, False),
    ]
    try:
        for nome, bruto, duracao, aberto, falhar in cenarios:
            print(f"\n--- {nome} ---")
            ia.falhar = falhar
            for _ in range(duracao):
                agora = base + timedelta(seconds=tempo[0])
                if aberto:
                    agora = agora.replace(hour=12)
                resultado = monitor.atualizar(bruto, agora=agora)
                while not monitor.pedidos.empty():
                    pedido = monitor.pedidos.get_nowait()
                    monitor.analisar_pedido(pedido)
                    monitor.pedidos.task_done()
                print(f"t={tempo[0]:.0f}s ADC={bruto} estado={resultado.estado}")
                while not monitor.avisos.empty():
                    aviso = monitor.avisos.get_nowait()
                    print(f"AVISO [{aviso['fonte']}]: {aviso['mensagem']}")
                    monitor.avisos.task_done()
                tempo[0] += 1
    finally:
        monitor.encerrar()
    print(f"\nRegistros: {registro.pasta}. Origem: simulador. LLM real não exercitada.")
    return monitor


def main():
    parser = argparse.ArgumentParser(description="MADARA / Energia que dá Lucro")
    parser.add_argument("--demonstracao", "--simular", action="store_true", help="ensaio sem hardware ou IA real")
    parser.add_argument("--voz", action="store_true", help="fala os avisos sem ativar microfone")
    parser.add_argument("--microfone", action="store_true", help="comandos Madara e diálogo de expediente (exige --voz)")
    parser.add_argument("--segundos", type=float, help="tempo máximo do monitor físico")
    parser.add_argument("--pasta", type=Path, help="pasta dos registros físicos, da simulação ou exportação")
    parser.add_argument("--exportar-pendentes", action="store_true", help="exporta com potência/tarifa configuradas")
    parser.add_argument("--diagnostico-ia", action="store_true", help="consulta modelos do provedor, sem geração de texto ou Arduino")
    parser.add_argument("--voz-motor", choices=("auto", "edge", "pyttsx3"), help="motor de síntese; pyttsx3 fala offline")
    args = parser.parse_args()
    if args.microfone and (not args.voz or args.demonstracao or args.exportar_pendentes or args.diagnostico_ia):
        parser.error("--microfone exige --voz no modo físico, sem demonstração, exportação ou diagnóstico.")
    if args.voz_motor and not args.voz:
        parser.error("--voz-motor exige --voz.")
    if args.diagnostico_ia:
        if args.demonstracao or args.exportar_pendentes or args.voz:
            parser.error("Execute --diagnostico-ia separadamente dos outros modos.")
        from modulos.config import obter_variavel
        from modulos.ia_generativa import IAGenerativa
        from modulos.ia_iluminacao import diagnosticar_erro_ia
        cfg = ConfigEnergia.do_ambiente()
        ia = IAGenerativa(provedor=obter_variavel("MAKER_IA_PROVEDOR", "groq"))
        try:
            resultado = ia.diagnosticar_conexao(timeout_s=cfg.ia_timeout_s)
        except Exception as erro:
            falha = diagnosticar_erro_ia(erro)
            print(f"[IA] {falha['codigo']} | HTTP={falha['http_status']} | {falha['orientacao']}")
            raise SystemExit(1)
        print(f"[IA] Provedor={resultado['provedor']} | conexão={resultado['conexao']} | modelo disponível={resultado['modelo_disponivel']}")
        print("Consulta somente de modelos. Inferência/geração de análise não foi testada.")
        return
    if args.segundos is not None and not 0 < args.segundos <= 86400:
        parser.error("--segundos deve estar entre 0 e 86400.")
    if args.demonstracao and args.exportar_pendentes:
        parser.error("Demonstração e exportação são operações separadas.")
    if args.demonstracao:
        demonstrar(args.pasta)
        return
    if args.exportar_pendentes:
        cfg = ConfigEnergia.do_ambiente()
        registro = RegistroEnergia(args.pasta)
        print(f"Eventos exportados: {registro.exportar_pendentes(cfg.potencia_kw, cfg.tarifa_rs_kwh)}")
        return
    from modulos.arduino import Arduino
    arduino = Arduino()
    voz = None
    try:
        if args.voz:
            from modulos.voz import Voz
            Voz.verificar_sintese()
            opcoes_voz = {"motor": args.voz_motor} if args.voz_motor else {}
            voz = Voz(arduino=arduino, escutar=args.microfone, **opcoes_voz)
        opcoes_registro = {"pasta": args.pasta} if args.pasta else {}
        if args.microfone:
            opcoes_registro["microfone"] = True
        executar_monitoramento(arduino, voz=voz, segundos=args.segundos, **opcoes_registro)
    finally:
        try:
            if voz:
                voz.fechar()
        finally:
            arduino.fechar()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError) as erro:
        raise SystemExit(f"[Maker Lab] {erro}")
