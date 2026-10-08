"""Import current PE inventory once; dry-run unless --apply is supplied."""
import argparse
import ipaddress
import json
import re
from sqlalchemy import select
from ..db import session
from ..models import Device,SourceIdentity,ProbeTarget,ProbeProfileVersion,now,uid
from ..security import audit
from .config import DEFAULT_VERSION_ID,seed_defaults,safe_ip
from .organization import ensure_destination
from .service import lock,capacity,next_slot


def import_pe(db,apply=False):
    seed_defaults(db);lock(db)
    existing=list(db.scalars(select(ProbeTarget).where(ProbeTarget.archived.is_(False))))
    names={t.name.casefold() for t in existing};addresses={ip for t in existing for ip in t.approved_ips}
    config=db.get(ProbeProfileVersion,DEFAULT_VERSION_ID).config
    plan=[]
    for device in db.scalars(select(Device).where(Device.archived.is_(False)).order_by(Device.name)):
        if not re.match(r'^PE(?:[-_\s]|$)',device.name,re.I):continue
        identities=list(db.scalars(select(SourceIdentity).where(SourceIdentity.device_id==device.id,SourceIdentity.present.is_(True))))
        candidates=[]
        for identity in identities:
            for address in identity.addresses:
                try:
                    ip=safe_ip(address)
                    candidates.append((ipaddress.ip_address(ip).version!=4,identity.source!='librenms',ip))
                except ValueError:continue
        if not candidates:plan.append({'name':device.name,'status':'sin_ip'});continue
        ip=sorted(candidates)[0][2]
        if device.name.casefold() in names or ip in addresses:
            plan.append({'name':device.name,'status':'ya_registrado'});continue
        item=ProbeTarget(id=uid(),name=device.name,address=ip,family='ipv4' if ipaddress.ip_address(ip).version==4 else 'ipv6',
            pop_id=device.pop_id,description='Importado del inventario PE',categories=['red_propia'],service='PE',profile_version_id=DEFAULT_VERSION_ID,
            overrides={},effective=dict(config),approved_ips=[ip],enabled=True,version=1,epoch=1)
        item.next_due=next_slot(item,now());db.add(item);ensure_destination(db,item);db.flush()
        capacity(db)
        audit(db,'probe_pe_imported',None,item.id)
        names.add(device.name.casefold());addresses.add(ip)
        plan.append({'name':device.name,'address':ip,'pop_assigned':bool(device.pop_id),'status':'creado' if apply else 'previsto'})
    if apply:db.commit()
    else:db.rollback()
    return plan


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    with session() as db:print(json.dumps(import_pe(db,args.apply),ensure_ascii=False))
