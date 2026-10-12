"""Persist a reviewed catalog of one category (hyperscalers, games...); validate before activating targets."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import ipaddress
import json
from pathlib import Path
from pydantic import BaseModel,Field,ConfigDict,model_validator
from sqlalchemy import select,func
from ..db import session
from ..models import ProbeCatalog,ProbeTarget,ProbeProfileVersion,now,uid
from ..security import audit
from .config import (host,TransportConfig,DEFAULT_VERSION_ID,TRANSPORT_VERSION_ID,GAMES_VERSION_ID,seed_defaults)
from .catalog import CATEGORIES
from .organization import ensure_destination
from .service import lock,capacity,next_slot,snapshot,changed
from .resolver import resolve_task,ProbeError
from . import fping,transport

PROVIDERS={'AWS':'AWS','GOOGLE':'GCP','MICROSOFT':'Azure','ORACLE':'Oracle','IBM':'IBM'}
CATEGORY_BY_NAME={c['name']:c['id'] for c in CATEGORIES}
# ICMP profile per category; a provider's border router answers from its control plane and keeps the standard one.
ICMP_VERSIONS={'juegos':GAMES_VERSION_ID}
DATA=Path(__file__).parent/'data'
# Loaded and activated on every start, in priority order under the capacity budget. Hyperscalers v2 keeps only the
# regions the ISP's traffic reaches (Bogotá, US East/South, México, São Paulo); v1 stays as the full reference.
STARTUP=[(name,True) for name in ('juegos-v1.json','dns-v1.json','mensajeria-v1.json','social-v1.json','streaming-v1.json',
    'banca-v1.json','gobierno-v1.json','voip-v1.json','saas-v1.json','search-ai-v1.json','hyperscalers-v2.json')]
DESCRIPTIONS={'border':'Router de borde del proveedor, no el servidor: responde ICMP desde su plano de control y puede limitarlo. '
                       'Sirve para seguir la entrada a su red cuando los servidores no responden.'}


class CatalogTarget(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=120)
    target:str=Field(min_length=1,max_length=253)
    protocol:str=Field(pattern='^(icmp|icmp6|tcp|https)$')
    port:int|None=Field(default=None,ge=1,le=65535)
    city:str|None=None
    country:str|None=Field(default=None,pattern='^[A-Z]{2}$')
    region:str|None=None
    scope:str=Field(pattern='^(regional|anycast|edge|api|border)$')
    asn:int|None=Field(default=None,ge=1,le=4294967295)  # when the endpoint is not in the provider's own network

    @model_validator(mode='after')
    def valid(self):
        self.target=host(self.target)
        if self.protocol in ('tcp','https') and self.port is None:raise ValueError('Falta puerto')
        return self


class Provider(BaseModel):
    model_config=ConfigDict(extra='forbid')
    provider:str
    asn:int=Field(ge=1,le=4294967295)
    service:str|None=Field(default=None,min_length=1,max_length=120)
    targets:list[CatalogTarget]=Field(min_length=1,max_length=200)


class Catalog(BaseModel):
    model_config=ConfigDict(extra='forbid')
    category:str
    version:int=Field(ge=1)
    providers:list[Provider]=Field(min_length=1,max_length=20)

    @model_validator(mode='after')
    def valid(self):
        names=[t.name.casefold() for p in self.providers for t in p.targets]
        if len(names)!=len(set(names)) or len(names)>200:raise ValueError('Destinos duplicados o demasiados')
        if self.category not in CATEGORY_BY_NAME or self.category=='RED PROPIA':raise ValueError('Categoría no válida')
        providers=[p.provider for p in self.providers]
        if len(providers)!=len(set(providers)):raise ValueError('Proveedor duplicado')
        if self.category=='HYPERSCALERS' and set(providers)-set(PROVIDERS):raise ValueError('Proveedor no válido')
        if self.category!='HYPERSCALERS' and any(not p.service for p in self.providers):raise ValueError('Falta el servicio del proveedor')
        return self


def retire(db,category,keys):
    """A newer version archives the checks it drops; one an operator activated keeps measuring until they archive it."""
    for target in db.scalars(select(ProbeTarget).where(ProbeTarget.catalog_key.like(category+':%'),ProbeTarget.archived.is_(False))):
        if target.catalog_key in keys or target.enabled:continue
        target.archived=True;target.version+=1;changed(db,target,'target_archived')
        audit(db,'probe_catalog_target_retired',None,target.id)


def persist(document):
    catalog=Catalog(**document);digest=hashlib.sha256(json.dumps(document,sort_keys=True).encode()).hexdigest()
    ids=[];category=CATEGORY_BY_NAME[catalog.category]
    with session() as db:
        seed_defaults(db);lock(db)
        source=db.get(ProbeCatalog,category)
        if source and (source.version>catalog.version or (source.version==catalog.version and source.digest!=digest)):
            raise ValueError('El catálogo guardado difiere. Publica el cambio con una versión mayor.')
        if source and source.version<catalog.version:
            retire(db,category,{f'{category}:{p.provider}:{t.name}' for p in catalog.providers for t in p.targets})
            source.version=catalog.version;source.document=document;source.digest=digest
        if not source:db.add(ProbeCatalog(id=category,version=catalog.version,document=document,digest=digest))
        for provider in catalog.providers:
            service=provider.service or PROVIDERS[provider.provider]
            for item in provider.targets:
                key=f'{category}:{provider.provider}:{item.name}'
                target=db.scalar(select(ProbeTarget).where(ProbeTarget.catalog_key==key))
                if target:
                    if not target.archived:ids.append(target.id)
                    continue
                if db.scalar(select(ProbeTarget.id).where(func.lower(ProbeTarget.name)==item.name.lower(),ProbeTarget.archived.is_(False))):
                    raise ValueError('Ya existe un destino con el nombre '+item.name)
                protocol='icmp' if item.protocol=='icmp6' else item.protocol
                version=TRANSPORT_VERSION_ID if protocol!='icmp' else DEFAULT_VERSION_ID if item.scope=='border' else ICMP_VERSIONS.get(category,DEFAULT_VERSION_ID)
                categories=[category]
                if item.name.startswith('GOOGLE-DNS'):categories.append('dns')
                if item.name=='GOOGLE-WEB':categories.append('search_ai')
                if item.name=='MICROSOFT-365':categories.append('saas')
                target=ProbeTarget(id=uid(),name=item.name,address=item.target,family='ipv6' if item.protocol=='icmp6' else 'ipv4',
                    protocol=protocol,check_config=TransportConfig(port=item.port or 443).model_dump() if protocol!='icmp' else {},
                    dns_policy='public_dns',catalog_key=key,catalog_metadata={
                        'provider':service,'asn_reference':item.asn or provider.asn,'region':item.region,
                        'city':item.city,'country':item.country,'scope':item.scope,'catalog_version':catalog.version,
                        'measurement_scope':'provider_border' if item.scope=='border' else 'endpoint_connectivity'},
                    validation={'status':'pending'},categories=categories,service=service,
                    description=DESCRIPTIONS.get(item.scope,'Conectividad al endpoint del servicio; no representa toda la región.'),
                    profile_version_id=version,effective=dict(db.get(ProbeProfileVersion,version).config),
                    overrides={},approved_ips=[],enabled=False,epoch=1,version=1)
                db.add(target);ensure_destination(db,target);db.flush();ids.append(target.id)
                audit(db,'probe_catalog_target_created',None,target.id)
        if db.scalar(select(func.count()).select_from(ProbeTarget).where(ProbeTarget.archived.is_(False)))>200:
            raise ValueError('Límite de 200 destinos registrados')
        audit(db,'probe_catalog_imported',None,category);db.commit()
    return ids


def validate_one(key,activate):
    with session() as db:
        target=db.get(ProbeTarget,key)
        # Re-runs preserve operator changes, paused validated targets and archived targets.
        if not target or target.archived or target.enabled or (target.validation.get('status')=='validated' and (not activate or target.validation.get('activation_requested'))):
            return {'id':key,'status':'preserved'}
        version=target.version
        task={'address':target.address,'family':target.family,'approved_ips':target.approved_ips,
              'dns_policy':target.dns_policy,'config':snapshot(target)}
    addresses=[];results={};error=None
    try:
        addresses=resolve_task(task)
        results=fping.run(addresses,task['config']) if task['config'].get('protocol','icmp')=='icmp' else transport.run(addresses,task['address'],task['config'])
        if not results or any(r['received']!=r['sent'] for r in results.values()):error='endpoint_check_failed'
    except ProbeError as exc:error=exc.code
    except Exception:error='validation_failed'
    with session() as db:
        lock(db)
        target=db.scalar(select(ProbeTarget).where(ProbeTarget.id==key).with_for_update())
        if target.version!=version or target.archived:return {'id':key,'status':'configuration_changed'}
        target.validation={'status':'failed' if error else 'validated','at':now(),'error':error,'activation_requested':activate,
            'addresses':addresses,'rounds':[{'address':ip,**r} for ip,r in results.items()]}
        target.approved_ips=addresses;target.resolved_ips=addresses;target.resolved_at=now()
        if not error and activate:
            target.enabled=True;target.next_due=next_slot(target,now());db.flush()
            try:capacity(db)
            except ValueError:
                target.enabled=False;error='probe_capacity';target.validation={**target.validation,'status':'failed','error':error}
        target.version+=1;audit(db,'probe_catalog_target_validated',None,key);db.commit()
        return {'name':target.name,'status':target.validation['status'],'enabled':target.enabled,'error':error}


def load(path,activate):
    ids=persist(json.loads(Path(path).read_text()))
    print(json.dumps({'catalog':Path(path).name,'stored':len(ids)}),flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        for result in pool.map(lambda key:validate_one(key,activate),ids):print(json.dumps(result),flush=True)


def startup():
    """Idempotent: targets already validated, paused or archived are preserved; failed ones are retried."""
    failed=False
    for name,activate in STARTUP:
        # One catalog that cannot be stored (e.g. edited file without migration) must not block the others.
        try:load(DATA/name,activate)
        except Exception as exc:
            failed=True;print(json.dumps({'catalog':name,'error':str(exc)[:300]}),flush=True)
    return 1 if failed else 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path',nargs='?',default=str(DATA/'hyperscalers-v2.json'))
    parser.add_argument('--activate',action='store_true')
    parser.add_argument('--startup',action='store_true',help='Carga los catálogos de arranque con su política de activación')
    args=parser.parse_args()
    if args.startup:raise SystemExit(startup())
    load(args.path,args.activate)


if __name__=='__main__':main()
