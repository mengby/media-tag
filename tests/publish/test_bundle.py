from hashlib import sha256
import json
from pathlib import Path

import pytest
from media_contracts import Catalog, Manifest
from media_tag.publish.bundle import ExportSettings, build_bundle


def settings(sought, release="synthetic-release"):
    env=sought.env
    return ExportSettings(database=str(env.database.path),object_store=str(env.catalog.object_store.root),workspace=str(env.router.workspace),
                          actor_id=env.owner.actor_id,asset_ids=(sought.asset.asset_id,),release_id=release,producer_revision="a"*40)


def read_catalog(path, manifest):
    return Catalog.model_validate_json((path/"objects"/manifest.catalog.sha256).read_bytes())


def test_snapshot_exports_human_values_and_identical_bytes_without_mutations(sought,tmp_path):
    env=sought.env
    before=env.catalog.get_asset(sought.asset.asset_id,principal=env.owner)
    destination=tmp_path/"bundle"
    manifest=build_bundle(settings(sought),destination)
    catalog=read_catalog(destination,manifest)
    asset=catalog.assets[0]
    assert asset.asset_id == sought.asset.asset_id and asset.blob.sha256 == before.sha256
    assert any("合成圆形" in f.values and f.origin=="human_effective" for f in asset.fields)
    assert (destination/"objects"/asset.blob.sha256).read_bytes() == sought.original.read_bytes()
    assert env.catalog.get_asset(sought.asset.asset_id,principal=env.owner) == before
    assert not (destination/".snapshot.db").exists()
    assert not any(str(env.workspace).encode() in (destination/"objects"/ref.sha256).read_bytes() for ref in manifest.objects)


def test_verified_ai_draft_has_portable_report_and_frame_closure(modeled,tmp_path):
    m=modeled
    destination=tmp_path/"bundle"
    manifest=build_bundle(settings(m.sought),destination)
    catalog=read_catalog(destination,manifest)
    asset=catalog.assets[0]
    assert any("唯一画中文字" in f.values and f.origin=="ai_draft" for f in asset.fields)
    assert asset.evidence
    refs={b.sha256:b for b in manifest.objects}
    for evidence in asset.evidence:
        payload=json.loads((destination/"objects"/evidence.blob.sha256).read_bytes())
        assert payload["source_sha256"] == asset.blob.sha256
        assert all(frame["sha256"] in refs for frame in payload["frames"])
        assert payload["medical_confirmation"] is False
    for ref in manifest.objects:
        raw=(destination/"objects"/ref.sha256).read_bytes()
        assert len(raw)==ref.size_bytes and sha256(raw).hexdigest()==ref.sha256


def test_unreadable_asset_refused_and_no_complete_manifest(sought,tmp_path):
    spec=settings(sought).model_copy(update={"actor_id":"unrelated-person"})
    with pytest.raises(Exception):
        build_bundle(spec,tmp_path/"bundle")
    assert not (tmp_path/"bundle"/"manifest.json").exists()


def test_output_reuse_refused(sought,tmp_path):
    path=tmp_path/"bundle"
    build_bundle(settings(sought),path)
    before=(path/"manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        build_bundle(settings(sought),path)
    assert (path/"manifest.json").read_bytes()==before


def test_corrupt_draft_refuses_incomplete_publication(modeled,tmp_path):
    modeled.reports["ocr"].write_bytes(b"{}")
    path=tmp_path/"bundle"
    with pytest.raises(ValueError,match="evidence unavailable"):
        build_bundle(settings(modeled.sought),path)
    assert not (path/"manifest.json").exists()


def test_corrupt_frame_refuses_publication(modeled,tmp_path):
    payload=json.loads(modeled.reports["ocr"].read_text())
    frame=modeled.env.workspace/payload["frames"][0]["artifact"]["ref"]
    frame.write_bytes(b"changed frame")
    with pytest.raises(ValueError,match="evidence unavailable"):
        build_bundle(settings(modeled.sought),tmp_path/"bundle")


def test_confirmed_status_requires_and_preserves_bound_review_policy(sought,tmp_path):
    from media_tag.review.policy import basis
    from media_tag.catalog.database import utc_now
    env=sought.env
    policy=sought.reviews.policy
    with env.database.transaction(write=True) as c:
        row=env.catalog._authorize(c,sought.asset.asset_id,env.owner)
        # Synthetic storage fixture for policy/version matching, no business approval.
        c.execute("INSERT INTO index_confirmations(asset_id,source_sha256,basis_sha256,policy_sha256,decision,actor_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (row["asset_id"],row["blob_sha256"],basis(c,row,policy.digest),policy.digest,"confirm",env.owner.actor_id,"{}",utc_now()))
    assert env.catalog.get_asset(sought.asset.asset_id,principal=env.owner).index_status=="confirmed"
    with pytest.raises(ValueError,match="confirmation requires"):
        build_bundle(settings(sought),tmp_path/"without-policy")
    spec=settings(sought).model_copy(update={"template_dir":str(Path("asset-index-template").resolve()),"review":policy.config.model_dump(mode="json")})
    manifest=build_bundle(spec,tmp_path/"with-policy")
    assert read_catalog(tmp_path/"with-policy",manifest).assets[0].index_status=="confirmed"
