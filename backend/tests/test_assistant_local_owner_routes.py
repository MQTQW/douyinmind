"""Owner management is explicit local admin only; all state stays temporary."""
import hashlib

import pytest

from tests.test_assistant_registry_routes import local,authenticate,PREFIX


def migrated(client):
    authenticate(client)
    result=client.post(PREFIX+"/migrate-legacy",json={"confirm":True,"allow_import":True})
    assert result.status_code==200
    value=result.json()
    return value["user_id"],value["device_id"]


def target(user,device):
    return PREFIX+"/users/"+user+"/devices/"+device+"/local-owner-access"


def payload(**changes):
    result={"confirm":True,"query_all_local":True,"catalog_read_allowed":True,
        "public_reply_allowed":True,"grant_current_libraries":True}
    result.update(changes)
    return result


def test_owner_api_requires_real_management_confirmation_and_no_get_migration(local):
    client,path,_,_=local
    assert client.put(target("unknown","unknown"),json=payload()).status_code==401
    assert not path.exists()
    user,device=migrated(client)
    before=path.read_bytes()
    snapshot=client.get(PREFIX).json()
    assert not snapshot["users"][0]["devices"][0]["local_owner_access"]["configured"]
    assert path.read_bytes()==before
    configured=client.put(target(user,device),json=payload())
    assert configured.status_code==200
    assert configured.json()["local_owner_access"]["query_all_local"]
    result=client.get(PREFIX+"/users/"+user+"/devices/"+device+"/library-choices")
    assert result.status_code==200 and result.json()["catalog_count"]==2
    assert all(item["can_import"] for item in result.json()["choice_options"])


@pytest.mark.parametrize("change",[{"confirm":False},{"query_all_local":1},{"binding_id":"model-target"},
    {"url":"https://example.com"},{"instance_id":"remote"},{"can_import":True}])
def test_owner_api_strict_body_no_scope_injection(local,change):
    client,path,_,_=local
    user,device=migrated(client)
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    response=client.put(target(user,device),json=payload(**change))
    assert response.status_code==422
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_scope_list_requires_admin_and_bad_origin_is_denied(local):
    client,path,_,_=local
    user,device=migrated(client)
    client.headers.pop("authorization")
    assert client.get(PREFIX+"/users/"+user+"/devices/"+device+"/library-choices").status_code==401
    assert client.put(target(user,device),headers={"origin":"https://evil.example"},json=payload()).status_code==403


def test_reopening_registry_after_owner_write_is_readonly(local):
    client,path,_,_=local
    user,device=migrated(client)
    assert client.put(target(user,device),json=payload()).status_code==200
    before=path.read_bytes()
    assert client.get(PREFIX).status_code==200
    assert client.get(PREFIX+"/users/"+user+"/devices/"+device+"/library-choices").status_code==200
    assert path.read_bytes()==before
