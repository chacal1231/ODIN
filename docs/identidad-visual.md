# Identidad visual y temas ODIN

Aplicada el 25 de septiembre de 2026 a partir de la referencia proporcionada por el usuario.

## Paleta

- Azul marino: #082E54.
- Celeste: #87B8EB.
- Azul pálido: #E5F1FC.
- Dorado: #F5A623.

Uso (6 de octubre de 2026): el dorado queda reservado al logo. Las acciones usan azul (`--primary`: #1F5C96 en claro, #3F7FBF en oscuro) y los títulos que abren detalle se muestran como texto con subrayado al pasar el puntero, no como botones. El ámbar (`--warn`, `--notice-bg`) solo indica advertencia o aviso, nunca decoración.

Los colores funcionales de texto, superficie, borde y estado se definen mediante variables en frontend/src/style.css. El tema oscuro utiliza fondos marinos y textos claros; el claro utiliza superficies blancas/azul pálido y texto marino. No se invierte el símbolo mediante filtros.

## Logo

frontend/public/odin-eye.svg es una reconstrucción vectorial basada en la imagen de referencia, no el archivo original de marca. Representa el ojo, los dos cuervos, los radios y detalles dorados. El nombre ODIN y el lema se componen como texto accesible en frontend/src/branding.tsx. Si se facilita el SVG original, puede reemplazarse el recurso conservando los componentes.

## Preferencia

Selector Claro/Oscuro/Sistema disponible antes y después de iniciar sesión. La preferencia se guarda únicamente en localStorage bajo odin.theme, sin datos de acceso. Sistema sigue cambios del sistema operativo; la preferencia se sincroniza entre pestañas. Si el almacenamiento no está disponible, el cambio funciona durante la sesión de la página.

frontend/public/theme.js aplica el tema antes de renderizar la aplicación, compatible con la política CSP de scripts del mismo origen. No requiere scripts inline ni cambios en autenticación.

## Verificación

6 de octubre de 2026: compilación TypeScript/Vite correcta tras el cambio de paleta y la paginación de tablas; no se revisó visualmente en un navegador.


Compilación TypeScript/Vite correcta. Chromium comprobó login, panel con fixtures visuales, escritorio y móvil de 390 px, persistencia tras recarga y cambios dinámicos del tema del sistema. Sin errores JavaScript observados ni desbordamiento horizontal de página. Los fixtures solo interceptaron respuestas dentro del navegador de pruebas; no crearon usuarios ni datos en PostgreSQL.
