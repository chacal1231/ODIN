# Identidad visual y temas ODIN

Aplicada el 25 de septiembre de 2026 a partir de la referencia proporcionada por el usuario.

## Paleta

- Azul marino: #082E54.
- Celeste: #87B8EB.
- Azul pálido: #E5F1FC.
- Dorado: #F5A623.

Uso (6 de octubre de 2026, vigente con el tono de consola): el dorado queda reservado al logo. Las acciones usan azul (`--primary`: #1F5C96 en claro, #3F7FBF en oscuro) y los títulos que abren detalle se muestran como texto con subrayado al pasar el puntero, no como botones. El ámbar (`--warn`, `--notice-bg`) solo indica advertencia o aviso, nunca decoración.

Los colores funcionales de texto, superficie, borde y estado se definen mediante variables en frontend/src/style.css. El tema oscuro utiliza fondos marinos y textos claros; el claro utiliza superficies blancas/azul pálido y texto marino. No se invierte el símbolo mediante filtros.

## Tono de consola NOC (11 de octubre de 2026)

ODIN es una consola de operación, no un sitio de marca. Reglas:

- **Superficies neutras**: gris claro en el tema claro y grafito en el oscuro (`--bg`, `--surface`, `--border`). El azul marino, el celeste y el dorado de la marca quedan solo para el logo.
- **El color es solo para el estado**: `--ok` normal, `--warn` y `--notice-*` advertencia, `--crit` y `--error-*` crítico, y `--nodata`, un violeta apagado, para *sin datos*, que no es lo mismo que normal ni que pausado. Ningún elemento decorativo usa esos colores.
- **Densidad de consola**: texto base de 14 px, tablas con 7 px de relleno vertical y paneles de 16–18 px.
- **Datos de red en monoespaciada** (`--mono`, clase `.mono` o `<code>`): IPs, ASN, interfaces y códigos. Las celdas usan cifras de ancho fijo.
- **Cabecera**: hora UTC junto a la zona elegida (proveedores, logs y tickets hablan en UTC) y el nombre del ISP (`ODIN_ISP_NAME`) bajo ODIN.
- **Tema por defecto: Sistema**. Cada operador elige Claro u Oscuro.

## Navegación (11 de octubre de 2026)

- **Inicio es la bandeja de ODIN**: *Necesita de ti* (lo crítico primero y, dentro de cada nivel, lo más antiguo; la salud de ODIN después de la red), *En curso* y *Resuelto en el turno* (12 h), con la salud de ODIN como fila de indicadores. El tablero anterior sigue en la pestaña **Panorama**. Los datos salen de `GET /api/v1/inbox`.
- **Los detalles se abren en un panel lateral** (`drawer.tsx`): casos, incidentes, comprobaciones y OLTs, sin perder la lista. Se cierra con Esc, con ✕ o con un clic fuera.
- **Enlaces directos**: `#caso=`, `#incidente=`, `#olt=` y `#comprobacion=<id>` abren el detalle en su vista. Cada panel tiene **Copiar enlace**.
- **El menú muestra los pendientes** sin reconocer de Casos, Incidentes y OLTs.
- **La cabecera** tiene el reloj y el usuario; tema, zona horaria, Mi acceso y Salir están en el menú del usuario.

Contraste comprobado (WCAG AA, al menos 4.5:1) para texto, texto secundario, insignias de estado y botones en ambos temas.

## Logo

frontend/public/odin-eye.svg es una reconstrucción vectorial basada en la imagen de referencia, no el archivo original de marca. Representa el ojo, los dos cuervos, los radios y detalles dorados. El nombre ODIN y el lema se componen como texto accesible en frontend/src/branding.tsx. Si se facilita el SVG original, puede reemplazarse el recurso conservando los componentes.

## Preferencia

Selector Claro/Oscuro/Sistema disponible antes y después de iniciar sesión. La preferencia se guarda únicamente en localStorage bajo odin.theme, sin datos de acceso. Sistema sigue cambios del sistema operativo; la preferencia se sincroniza entre pestañas. Si el almacenamiento no está disponible, el cambio funciona durante la sesión de la página.

frontend/public/theme.js aplica el tema antes de renderizar la aplicación, compatible con la política CSP de scripts del mismo origen. No requiere scripts inline ni cambios en autenticación.

## Verificación

6 de octubre de 2026: compilación TypeScript/Vite correcta tras el cambio de paleta y la paginación de tablas; no se revisó visualmente en un navegador.


Compilación TypeScript/Vite correcta. Chromium comprobó login, panel con fixtures visuales, escritorio y móvil de 390 px, persistencia tras recarga y cambios dinámicos del tema del sistema. Sin errores JavaScript observados ni desbordamiento horizontal de página. Los fixtures solo interceptaron respuestas dentro del navegador de pruebas; no crearon usuarios ni datos en PostgreSQL.
