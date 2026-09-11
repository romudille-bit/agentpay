"""The public discovery surfaces (llms.txt, .well-known manifest) and the Stacks rail."""
def test_discovery_surfaces_name_stacks_when_enabled(monkeypatch):
    """llms.txt and the well-known manifest advertise the Stacks rail only
    when sBTC settlement is actually configured on the deployment."""
    from gateway.routes import discovery
    monkeypatch.setattr(discovery.settings, "STACKS_ENABLED", True)
    monkeypatch.setattr(discovery.settings, "STACKS_GATEWAY_ADDRESS",
                        "SP23XKWSEQ9D4CVPT0H39N2TYVEE5AJECPKW6CZ3C")
    monkeypatch.setattr(discovery.settings, "STACKS_NETWORK", "mainnet")
    txt = discovery.build_llms_txt()
    assert "sBTC on Stacks mainnet" in txt and "live: stacks-mainnet" in txt
    assert 'agentpay-x402[stacks]' in txt
    assert discovery._stacks_networks() == ["stacks-mainnet"]

    monkeypatch.setattr(discovery.settings, "STACKS_ENABLED", False)
    assert "not enabled on this deployment" in discovery.build_llms_txt()
    assert discovery._stacks_networks() == []
