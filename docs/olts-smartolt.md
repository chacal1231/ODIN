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

## Casos e impacto

Con estos datos, el correlador abre **un caso de acceso por OLT**. El caso reúne sus caídas de PON, uplinks y alcance, con una lectura para el N1: OLT inalcanzable, uplink caído, corte de fibra común o en un PON, falla de energía o ONUs sin conexión. Las ONUs de SmartOLT son también la base del **impacto en clientes** de todos los casos: confirmado cuando SmartOLT reporta caídas y potencial cuando falla algo de lo que cuelgan sus OLTs. Reglas completas: [Casos y veredictos](casos-y-veredictos.md).

Para que ODIN sepa de qué router e interfaz cuelga cada OLT, la OLT debe estar también en LibreNMS (ODIN la asocia por la IP que reporta SmartOLT) y alguna de sus direcciones debe caer dentro de la subred de un puerto de router.
