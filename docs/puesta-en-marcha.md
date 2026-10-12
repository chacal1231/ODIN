# Puesta en marcha

Después del primer ingreso, un administrador abre **Configuración → Puesta en marcha**. Mientras falten pasos obligatorios, la bandeja de Inicio lo recuerda. El asistente sigue un orden en el que cada fuente le da sentido a la siguiente. Cada paso prueba la conexión y muestra lo que encontró. Su estado sale de lo que ODIN ya tiene: el asistente no guarda nada propio y se puede retomar en cualquier momento.

| # | Paso | Obligatorio | Qué aporta |
|---|---|---|---|
| 1 | LibreNMS | Sí | Equipos, puertos con su IP y la **descripción de cada puerto** |
| 2 | Proveedores detectados | Sí | Tránsitos, transportes, IXP y peering, leídos de esas descripciones |
| 3 | Zabbix | No | Hosts y sus alertas, que ODIN asocia a los casos |
| 4 | Graylog | Sí | Logs de BGP e interfaces de los routers: de ahí nacen los casos |
| 5 | SmartOLT | No | OLTs, puertos PON, uplinks y caídas de PON |
| 6 | Destinos de monitoreo | Sí | Sondeos propios hacia los equipos y hacia cada enlace con proveedores |
| 7 | Telegram | No | Avisos en el grupo del NOC y un hilo por cada falla con un proveedor |
| 8 | Modelo del analista | No | Explicación de cada caso citando la evidencia |

## Antes de empezar: credenciales de solo lectura

ODIN solo lee. Conviene darle a cada fuente una credencial que **tampoco pueda escribir**:

- **LibreNMS**: un usuario de solo lectura (*global read*) y un token de API de ese usuario.
- **Zabbix**: un usuario con permiso de lectura sobre los grupos de hosts de la red y un token de API (Zabbix 5.4 o posterior). ODIN lo envía como lo espera cada versión: encabezado `Authorization: Bearer` desde 6.4 (obligatorio en 7.2) y campo `auth` antes.
- **Graylog**: un token de un usuario con lectura sobre el stream de los routers.
- **SmartOLT**: la API key de cliente. Si SmartOLT lo permite, restringirla a la IP del servidor ODIN.

Si el ISP no tiene alguna de estas fuentes, [`stack/`](../stack/README.md) instala LibreNMS, Zabbix o Graylog, cada uno en su servidor. Su script crea la credencial de solo lectura y muestra el endpoint, la red permitida y el token para este asistente.

En LibreNMS, Zabbix y Graylog también se indican las **redes permitidas**: la IP, o la red, donde vive ese servidor. ODIN se niega a conectarse a cualquier otra dirección, aunque el DNS apunte a otro lado. SmartOLT, DeepSeek y Telegram son servicios públicos: ODIN solo acepta direcciones públicas para ellos.

## Paso 2: cómo reconoce ODIN a los proveedores

ODIN no tiene una lista aparte de proveedores: los lee de **la descripción (ifAlias) de cada puerto** de los routers, que LibreNMS trae. La primera palabra es el rol y la segunda, el proveedor:

| Rol | Ejemplo de descripción |
|---|---|
| Tránsito | `TRANSITO-CARRIER-100G` |
| Transporte | `TRANS-TX-POP-NORTE-RUTA-01`, `TRANSPORTE-CENTRO-NORTE` |
| IXP | `PIT-IXPBOG`, `IXP-NAP` |
| Peering | `PNI-CDN-01-100G`, `PTP-PROVEEDOR`, `PEERING-X` |
| Cliente | `CUST: Nombre {DIA:DIA-0001 \| ASN:NA \| CT:ISP}`, `DIA-0009-nombre` |
| CDN interno | `OCA01-…`, `CDN-…` |

- Un **transporte** se muestra con su descripción completa, porque el proveedor no siempre es la segunda palabra.
- Si la subred de un puerto contiene la IP de **otro equipo propio**, ODIN lo trata como **enlace interno**, aunque vaya por el transporte de un tercero.
- El asistente lista los **puertos con IP sin rol reconocido**. Si alguno es un enlace con proveedores, hay que corregir su descripción en el router: LibreNMS la trae en la siguiente sincronización y el paso se actualiza solo.

Con esta información, ODIN:
- marca como críticos los casos de tránsito y transporte;
- dice por qué enlace sale cada traza ("sale por tránsito CARRIER · PE-A Ethernet51/1"), y usa ese enlace como **salida** en el veredicto de los servicios externos;
- calcula el **impacto** de una interfaz caída con las OLTs que cuelgan de ella;
- abre un hilo en el grupo de proveedores de Telegram por cada falla.

## Paso 6: destinos sugeridos

ODIN propone, sin crear nada hasta que se confirme:

- **La IP de gestión de cada equipo propio**, con la plantilla *PE / infraestructura propia*.
- **El otro extremo de cada enlace con un proveedor**, es decir, la otra dirección de su `/31`, `/127` o `/30` IPv4, con la plantilla *Enlaces y tránsito*. Una `/126` o una red más grande no se sugiere: habría que adivinar cuál es la dirección del vecino.

No sugiere lo que ya está monitoreado. Lo que se crea pasa por las mismas validaciones que cualquier comprobación (capacidad del sondeador, límite de 200 y nombres únicos), y queda activo y asociado al POP del equipo.

## Comunicación del NOC

El asistente cubre Telegram. El resto de la comunicación se configura en **Configuración → Comunicación NOC**: la vinculación de WhatsApp por QR y la elección del grupo, el plazo para informar, los turnos y las guías para N1 ([Comunicación del NOC](comunicacion-noc.md)).

## Después de la puesta en marcha

Una lista para la primera semana:

1. **Observar antes de publicar.** Mantener sin vincular (o en pausa) los grupos del NOC de Telegram y WhatsApp durante una semana. En **Casos**, la calibración muestra cuántos casos habría avisado cada día y cuáles son los asuntos más ruidosos.
2. **Resolver con los DNS del ISP.** El servidor ODIN debe usar los recursivos del ISP, para que las CDN le entreguen el mismo nodo que a los clientes ([Configuración](configuracion.md)).
3. **Revisar los catálogos** de servicios externos con los flujos del ISP, sobre todo juegos, banca y gobierno ([Catálogos](catalogos.md)).
4. **Aprobar los umbrales adaptativos.** Desde las 24 horas aparecen propuestas provisionales y desde los 7 días, listas. Se aprueban por destino o todas juntas, y los destinos estables pueden pasar a modo automático ([Organización del monitoreo](organizacion-monitoreo.md#umbrales-adaptativos)).
5. **Verificar la topología de acceso.** Cada OLT debe estar en LibreNMS con una dirección dentro de la subred de un puerto de router, para que el impacto de una interfaz caída cuente sus ONUs ([OLTs con SmartOLT](olts-smartolt.md)).
6. **Ajustar las guías** para N1 a los procedimientos del ISP, y luego activar los avisos de casos, vincular o reanudar los grupos del NOC y entregar la [Guía para N1](guia-n1.md).
