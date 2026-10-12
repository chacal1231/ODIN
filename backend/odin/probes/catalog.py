"""Destination taxonomy; names are not probe endpoints."""
CATEGORIES = [
    {'id':'red_propia','name':'RED PROPIA','services':['PE']},
    {'id':'hyperscalers','name':'HYPERSCALERS','services':['AWS','Azure','GCP','Oracle','IBM']},
    {'id':'dns','name':'DNS','services':['Cloudflare','Google','Quad9','OpenDNS']},
    {'id':'juegos','name':'JUEGOS','services':['Riot','Steam/Valve','Epic','EA','Blizzard','PlayStation','Xbox','Free Fire','Roblox','Supercell','Tencent','Activision','Multiplay/Unity']},
    {'id':'streaming','name':'STREAMING','services':['YouTube','Netflix','Twitch','Disney+','Prime Video']},
    {'id':'social','name':'SOCIAL','services':['Meta/Facebook','Instagram','WhatsApp','TikTok','Telegram','X']},
    {'id':'cdn','name':'CDN','services':['Cloudflare','Akamai','Fastly','CloudFront']},
    {'id':'saas','name':'SAAS','services':['Microsoft 365','Google Workspace','GitHub','Dropbox','Salesforce']},
    {'id':'voip_rtc','name':'VOIP/RTC','services':['Teams','Zoom','Five9','Webex','Discord']},
    {'id':'banca_pagos','name':'BANCA/PAGOS','services':['PSE','Wompi','Mercado Pago'],'hint':'Añade el nombre de cada banco en Servicio.'},
    {'id':'gobierno','name':'GOBIERNO','services':['DIAN','GOV.CO'],'hint':'Añade otros servicios estatales en Servicio.'},
    {'id':'search_ai','name':'SEARCH/AI','services':['Google','Bing','ChatGPT','Gemini','Claude']},
    {'id':'ecommerce','name':'ECOMMERCE','services':['Amazon','Mercado Libre','Temu','AliExpress']},
    {'id':'mensajeria','name':'MENSAJERÍA','services':['WhatsApp','Telegram','Messenger']},
    {'id':'speedtest','name':'SPEEDTEST','services':['Ookla','Servidor propio','Servidor de terceros']},
    {'id':'internacional','name':'INTERNACIONAL','services':['Miami','Ashburn','São Paulo','Madrid','Frankfurt'],'hint':'Indica la ciudad o ubicación del destino de prueba.'},
]
CATEGORY_IDS = {item['id'] for item in CATEGORIES}
