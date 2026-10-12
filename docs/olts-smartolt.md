# OLTs con SmartOLT

ODIN lee las OLTs del ISP desde su cuenta de SmartOLT (una cuenta por instalación) y las muestra en **Monitoreo → OLTs**. Cubre el inventario, las caídas de PON y los casos de acceso que arma con ellas.

## Solo lectura

El conector solo puede llamar a una lista cerrada de endpoints `GET` de SmartOLT (`odin/olts/connector.py`, `READS`) y no tiene ningún otro método. ODIN no puede reiniciar, deshabilitar, mover ni borrar ONUs, ni cambiar la configuración de una OLT. Una prueba lo verifica.

Las ONUs se piden con una lista explícita de campos técnicos y solo esos se guardan. **No llegan a ODIN** la dirección, el contacto, el usuario y la contraseña PPPoE ni las IPs de gestión.

Solo se conecta por HTTPS a `<subdominio>.smartolt.com`, a una dirección pública, sin seguir redirecciones.

## Qué lee y cada cuánto

| Lectura | Frecuencia | Llamadas por hora (2 OLTs) |
|---|---|---|
| Caídas de PON (`get_outage_pons`, por OLT) | cada 5 min | 24 |
| Inventario: OLTs, uptime, temperatura, puertos PON, uplinks | cada hora | 6 |
| ONUs, paginadas de a 100, con estado y señal | una vez al día | ~0,4 |

Todo suma unas 30 llamadas por hora de las 1.000 de la cuenta. Cada lectura lleva un retraso aleatorio, como pide SmartOLT. Si la API responde 429, ODIN deja de leer hasta que pase el `Retry-After`. El presupuesto restante (`X-RateLimit-Remaining`) se muestra en la vista.

## Configuración

1. **Administración → Fuentes → SmartOLT · OLTs**: URL `https://tusubdominio.smartolt.com` y la API key de cliente. Luego **Probar conexión**.
2. **Monitoreo → OLTs**: indicar la zona horaria de la cuenta (las fechas de SmartOLT vienen en esa zona) y **Activar lectura**.
3. Cada OLT se asocia sola al equipo del inventario que tenga su IP, y hereda su POP. Un administrador puede fijar el POP a mano.

Si SmartOLT permite restringir la API key por IP, limitarla a la IP del servidor ODIN.

## Lo que se ve

- **Estado de cada OLT**: inalcanzable, uplink caído, caída de PON, PON caído o normal. También ONUs en línea, uptime y temperatura.
- **Caídas activas** según SmartOLT: corte de fibra (LOS), falla de energía, sin conexión, o de más de 7 días. Con ONUs afectadas, zona, splitters y hora de inicio.
- **Uplinks y puertos PON**: estado, ONUs en línea, señal media, potencia TX y cuántas ONUs tienen señal crítica o en advertencia, según la clasificación de SmartOLT.

Si la lectura de caídas lleva más de 20 minutos sin éxito, o si la API limita las llamadas, aparece en la barra de estado.

## Casos de acceso

El correlador abre **un caso por OLT** ([access.py](../backend/odin/cases/access.py)) cuando tiene caídas de PON, un uplink caído o SmartOLT no la alcanza, y lo da por recuperado cuando no queda ninguna. Cada caída, uplink y cambio de alcance queda como evidencia con su hora; el caso muestra los PON afectados, las ONUs, las zonas y los splitters. Las caídas de más de 7 días (stale) quedan fuera: ya son conocidas.

La lectura de ODIN, escrita para el nivel 1 que vigila e informa, sigue este orden:

| Situación | Lectura | Acción para N1 |
|---|---|---|
| SmartOLT no alcanza la OLT | La OLT, su energía o su uplink | Informar caída de la OLT; N2 revisa energía y uplink |
| Uplink caído | Causa probable de lo que muestran sus PON | Informar; N2 revisa el enlace hacia el POP |
| LOS en 2 o más PON que empiezan juntos (10 min) | Corte común (tramo o tarjeta) | Informar corte en las zonas; N2 despacha a campo |
| LOS en un PON | Corte en la distribución de ese PON | Informar corte en la zona; N2 despacha a campo |
| Falla de energía (power) | Las ONUs de los clientes perdieron energía | Informar falla eléctrica; no es la red, no requiere despacho |
| Sin conexión (offline) | Sin causa que SmartOLT pueda identificar | Informar y vigilar |

Es crítico (avisa en la barra y por Telegram) si la OLT no es alcanzable, si hay un uplink caído, si hay un corte común o si afecta a 64 ONUs o más. Los demás también aparecen en la bandeja, porque el N1 informa toda caída de acceso; el caso reemplaza a las caídas sueltas de esa OLT.

Con el inventario, la OLT se asocia a su equipo, y el caso muestra como relacionados la interfaz del router que llega a ella y los casos del mismo equipo o POP, así como las alertas de Zabbix o LibreNMS y los incidentes de sondeo de su gestión. El analista recibe la lectura y los PON con sus causas.
