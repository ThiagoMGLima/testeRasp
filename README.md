# Teste USB na Raspberry Pi

Copie **esta pasta inteira** para a Raspberry, por exemplo para
`~/vguard-usb-raspberry`. Também é fornecido um arquivo `.tar.gz` com o mesmo
conteúdo, pronto para transferir. Não copie ambientes `.venv` ou compiladores
do computador: este teste usa somente Python 3.10 ou superior e bibliotecas
padrão do Linux. Raspberry Pi OS com Python 3.11 atende ao requisito.

O firmware já está gravado no ESP32. Conecte o ESP32 a uma porta USB-A da Pi,
com cabo de dados. Alimente a Pi pela própria fonte. Faça o primeiro teste com
apenas USB, deixando CAN, microSD e ADXL345 desconectados. Feche outros leitores
e monitores seriais dessa porta.

## Executar

Se transferiu o arquivo compactado, extraia na pasta em que pretende guardar
o teste. Ele contém a pasta `vguard-usb-raspberry/`:

```bash
tar -xzf vguard-usb-raspberry.tar.gz
cd vguard-usb-raspberry
```

Se copiou a pasta inteira, apenas abra o terminal nela. Confira a versão do
Python e as portas disponíveis:

```bash
python3 --version
ls -l /dev/serial/by-id/
ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
```

O nome da porta na Pi pode ser diferente daquele usado no computador. Usando
`/dev/ttyUSB0` como exemplo, execute:

```bash
bash testar_usb.sh /dev/ttyUSB0 20
```

Se houver erro de permissão e a porta pertencer ao grupo `dialout`, faça na Pi:

```bash
sudo usermod -aG dialout "$USER"
newgrp dialout
```

No novo prompt, volte a esta pasta e repita o teste. Esse ajuste é na Pi,
independente da alteração de grupos já feita no computador.

## Interpretar

O teste dura aproximadamente 20 segundos e deve terminar com **USB: APROVADO**.
Isso exige STATUS do firmware `usb-sd-0.1.0`, protocolo 2, plataforma ESP32,
identificação de dados não sintéticos e sincronização PING/PONG recente.
Qualquer corrupção ou perda observada após a primeira sincronização reprova o
teste. Bytes rejeitados durante a abertura são registrados separadamente, sem
apagar os totais; esse diagnóstico não determina a causa dos bytes iniciais.

Sem os módulos, são esperados ADXL345 `failed`, CAN/OBD `can_failure`, SD
`aguardando` e zero amostras de aceleração/OBD. A aprovação é da comunicação
USB; funcionamento dos sensores, cartão, CAN e integração com visão são etapas
posteriores.

PING é um comando normal do protocolo. Caso o CAN esteja conectado e receba
tráfego, o firmware pode iniciar as consultas OBD previstas. Por isso o primeiro
teste é feito somente com USB.

Cada execução preserva em `execucoes/usb-DATA-HORA/`:

- `usb.bin`: bytes recebidos da placa;
- `status.jsonl`: diagnóstico periódico;
- `amostras.jsonl`: amostras recebidas, vazio quando os sensores estão ausentes;
- `resumo.json`: resultado, critérios e contadores.

Guarde o `resumo.json` para conferir o resultado. Se houver falha, o arquivo
informa os critérios que não foram atendidos. Ao terminar, a serial é fechada.
O teste não grava a flash e não precisa de conexão com internet.
