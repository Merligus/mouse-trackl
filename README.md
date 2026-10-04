# MouseTrack

Mede e acompanha **DPI**, **taxa de polling** e **uso** do mouse no Linux, lendo
`evdev` direto. Interface PySide6 + CLI. Sem dependências além do PySide6.

Feito para o `Compx ATK A9 Plus Nearlink` e seus dongles, mas funciona com
qualquer mouse que o kernel enxergue.

## Instalar

```sh
./install.sh
```

Coloca o `.desktop` e o ícone em `~/.local/share`, e o app aparece no menu do KDE
como **MouseTrack**. Avisa se você não estiver no grupo `input`.

## Usar

A interface abre sem argumentos:

```sh
./bin/mousetrack
```

Mesmos dados pelo terminal:

```sh
./bin/mousetrack list                    # mouses detectados agora
./bin/mousetrack status                  # resumo + valores salvos
./bin/mousetrack rate -s 10              # mede a taxa de polling
./bin/mousetrack dpi --cm 20             # calibra o DPI
./bin/mousetrack dpi --cm 20 --stage 2   # calibra e rotula um estágio
./bin/mousetrack sensitivity --set 0.2   # ajusta a sensibilidade
./bin/mousetrack health                  # checa o teclado fantasma
```

Calibrações ficam em `~/.config/mousetrack/config.json`, indexadas por
`vid:pid` — cabo e dongle guardam valores separados.

## O que dá pra saber, e por quê

Este mouse **não reporta** DPI, polling rate nem bateria. Nada disso sai de
leitura: o descritor HID declara só botões, eixos e roda.

### Taxa de polling — medida, confiável

O app cronometra o intervalo entre pacotes e procura o **período fundamental**.

A mediana simples estaria errada aqui. O mouse só manda pacote quando tem
movimento pra relatar, então uma mão que pausa produz intervalos que são
*múltiplos* do período, e a mediana cai no múltiplo que você produziu mais.
Em vez disso o estimador monta um histograma dos intervalos, acha o menor
agrupamento que se repete o bastante pra não ser ruído, e recentra uma janela
em torno dele — sempre abaixo de 2× o período, que é onde começa o primeiro
harmônico.

Validado contra dados sintéticos de 125 Hz a 8000 Hz, com até 80% de pausas:
erro abaixo de 0.3% em todos os casos, enquanto a mediana errava por 4×.

O campo **amostra** diz quanto do sinal caiu no período fundamental. Abaixo de
25% a leitura é um piso, não um número — mexa o mouse sem parar e repita.

Isso vale mais que ler o descritor: o `bInterval=1` do endpoint diz só que o
**teto** é 1000 Hz. Como o device negocia full-speed (12 Mbps), 1000 Hz é o
máximo físico de qualquer jeito — frames USB de 1 ms.

### DPI — calibração, não leitura

Conta os counts crus de `REL_X`/`REL_Y` enquanto você move uma distância física
conhecida: `DPI = counts / polegadas`.

Use régua ou a borda do mousepad. A aceleração do KDE não atrapalha — os counts
são lidos antes de qualquer processamento do compositor. Repita 2–3 vezes;
variação grande significa movimento torto ou mouse levantado no meio.

O campo **Rótulo** permite calibrar cada estágio do botão de DPI separadamente
(`estágio 1`, `estágio 2`, …) e ver todos listados depois.

### Sensibilidade — essa dá pra mudar

O DPI do firmware é trancado atrás do protocolo vendor fechado da Compx, então
**não dá pra mudar o DPI do hardware por aqui**. O que dá, e é o que muda a
velocidade do ponteiro na prática, é o multiplicador do libinput.

O KWin publica um objeto D-Bus por dispositivo de entrada, com as configurações
como propriedades de leitura e escrita:

```
/org/kde/KWin/InputDevice/event14
    pointerAcceleration             double, -1.0 .. 1.0
    pointerAccelerationProfileFlat  bool
```

Escrever nelas aplica na hora. O app faz isso e, em paralelo, espelha o valor em
`kcminputrc` para sobreviver a logout e replug:

```
[Libinput][14139][4373][Compx ATK A9 Plus Nearlink]
PointerAcceleration=0.350
PointerAccelerationProfile=0
```

Os ids vão em decimal: `14139` = `0x373B`, `4373` = `0x1115`.

Perfil **adaptativo** acelera conforme a velocidade do movimento; **plano** é um
multiplicador constante, que é o que você quer se estiver mirando em jogo.
O botão **Restaurar** volta ao `defaultPointerAcceleration` do próprio device.

Dois detalhes que custaram uma ida e volta:

- Escrever `kcminputrc` e chamar `reconfigure` **não funciona**. O KWin lê a
  configuração por dispositivo quando o dispositivo aparece, e `reconfigure`
  nunca revisita isso. A propriedade D-Bus é o que surte efeito; o arquivo é só
  persistência.
- `qdbus6` falha nesses objetos com *"No such interface"* mesmo com a
  introspecção listando a interface. Por isso o app usa `gdbus`.

### Bateria — indisponível, e não é limitação do código

Nenhum dos devices declara HID Power Page (`0x84`/`0x85`) ou Battery Strength.
Não existe `/sys/class/power_supply/` pra eles, e sondar os feature reports
vendor (`0xFF02` reports `0x13`/`0x08`, `0xFF04` report `0x06`) devolve lixo —
zeros e `ff ff`, sem nem ecoar o report ID.

O nível só sai pelo protocolo proprietário da Compx. O caminho pra obter:
capturar o tráfego USB do `ATK Hub` no Windows com Wireshark/USBPcap, achar o
report que carrega o nível, e reimplementar. Até lá o app diz honestamente que
não tem, em vez de inventar.

## Vigia do teclado fantasma

O rodapé da janela (e `mousetrack health`) confere se a interface HID 00 do ATK
continua desligada do `usbhid`.

Essa interface é um teclado boot falso. No cabo ela dispara, logo após um
clique, um report com modificador `0x64` = LeftAlt + RightShift + AltGr, e o
AltGr trava em auto-repeat até o cabo sair — o que inutiliza o teclado de
verdade. Capturado em 2026-10-03: **748 auto-repeats ao longo de 26 segundos**.

A correção mora em `/etc/udev/rules.d/99-atk-a9-no-fake-keyboard.rules`, que
desliga o `usbhid` dessa interface a cada plug. Se alguém remover a regra, o
app avisa em vermelho.

## Topologia do hardware

```
1-11.1  3554:fa09  dongle 2.4G combo (teclado + mouse)
        iface 0 -> teclado boot
        iface 1 -> consumer + system + teclado NKRO + mouse (report id 0x07)

1-11.2  373b:1115  ATK A9 Plus no cabo
        iface 0 -> teclado boot falso    <- desligado pela regra udev
        iface 1 -> consumer + system + teclado NKRO + vendor 0xFF02/0xFF04
        iface 2 -> mouse: 5 botões, roda, tilt, movimento

        373b:10c9  dongle Nearlink só do mouse (aparece no kcminputrc)
```

São três caminhos para o mesmo mouse físico, com `vid:pid` diferentes — por isso
o seletor da janela às vezes mostra mais de um, e por isso as calibrações não se
misturam.

## Estrutura

```
bin/mousetrack        launcher
mousetrack/core.py    evdev: descoberta, leitura, estimadores
mousetrack/ui.py      interface PySide6
mousetrack/cli.py     comandos de terminal
mousetrack/kde.py     sensibilidade via kcminputrc
mousetrack/store.py   calibrações salvas
mousetrack/health.py  vigia do teclado fantasma
```

Uma thread de fundo (`EventPump`) é dona de todos os event nodes e acumula as
estatísticas; os widgets só renderizam um retrato desse estado 4× por segundo.
Toda medição sai do mesmo fluxo, então "Medir" e "Calibrar" nunca precisam
tomar os dispositivos pra si. A thread também sobrevive a hotplug: ela reescaneia
a cada 2 s e reconstrói os leitores quando o conjunto de devices muda.

## Requisitos

- Python 3.10+
- PySide6 (para a interface; a CLI funciona sem)
- Estar no grupo `input`: `sudo usermod -aG input $USER`, depois relogar

## Licença

MIT — veja [LICENSE](LICENSE).

O `CLAUDE.md` guarda a razão das decisões de projeto e as armadilhas que
custaram depuração de verdade (o estimador de polling, o D-Bus do KWin, o
teclado fantasma). Leia antes de mexer no `core.py` ou no `kde.py`.
