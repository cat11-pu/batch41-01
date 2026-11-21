import hashlib
import ipaddress
import json


class AdjacencyGuard:
    """OSPF adjacency authentication, LSA validation and state machine."""

    AUTH_LEN = 16
    MAX_AGE = 3600
    BACKBONE = '0.0.0.0'
    STATES = ['Down', 'Init', 'TwoWay', 'ExStart', 'Exchange', 'Loading', 'Full']

    def __init__(self, router_id='1.1.1.1', area_id='0.0.0.0', auth_key='s3cret', interfaces=None, max_retransmit=3, allow_external=False):
        self.router_id = router_id
        self.area_id = area_id
        self.auth_key = auth_key
        self.interfaces = interfaces if interfaces is not None else {'eth0': '10.0.0.0/24'}
        self.max_retransmit = max_retransmit
        self.allow_external = allow_external
        self.neighbors = {}
        self.lsdb = {}
        self.audit = []
        self.dd_seq = {}
        self.lsr_counts = {}

    def _record(self, accepted, reason, actor=None):
        entry = {'accepted': accepted, 'reason': reason, 'actor': actor}
        self.audit.append(entry)
        return entry

    def _reject(self, reason, actor=None):
        self._record(False, reason, actor)
        return {'accepted': False, 'reason': reason}

    def _accept(self, actor=None, **extra):
        self._record(True, 'ok', actor)
        result = {'accepted': True, 'reason': 'ok'}
        result.update(extra)
        return result

    def register_neighbor(self, router_id, state='Down'):
        self.neighbors[router_id] = state
        return {'ok': True, 'router_id': router_id, 'state': state}

    def on_hello(self, packet):
        actor = packet.get('router_id')
        if packet.get('auth_len') != self.AUTH_LEN:
            return self._reject('bad auth length', actor)
        if packet.get('auth') != self.auth_key:
            return self._reject('auth mismatch', actor)
        if packet.get('area_id') != self.area_id:
            return self._reject('area mismatch', actor)
        interface = packet.get('interface')
        if interface not in self.interfaces:
            return self._reject('unknown interface', actor)
        source = packet.get('source')
        try:
            in_subnet = ipaddress.ip_address(source) in ipaddress.ip_network(self.interfaces[interface])
        except ValueError:
            in_subnet = False
        if not in_subnet:
            return self._reject('subnet mismatch', actor)
        if actor == self.router_id:
            return self._reject('self router id', actor)
        if actor not in self.neighbors:
            self.neighbors[actor] = 'Init'
        return self._accept(actor, state=self.neighbors[actor])

    def on_dd(self, packet):
        actor = packet.get('router_id')
        if packet.get('area_id') is not None and packet.get('area_id') != self.area_id:
            return self._reject('area mismatch', actor)
        seq = packet.get('seq')
        last = self.dd_seq.get(actor)
        if last is not None:
            if seq == last:
                return self._reject('dd sequence replay', actor)
            if seq < last:
                return self._reject('dd sequence wrapped', actor)
        self.dd_seq[actor] = seq
        return self._accept(actor)

    def _validate_lsa_integrity(self, packet):
        if not packet.get('checksum'):
            return 'invalid lsa checksum'
        age = packet.get('age')
        if not isinstance(age, int) or age <= 0 or age >= self.MAX_AGE:
            return 'lsa age out of range'
        return None

    def on_lsa(self, packet):
        actor = packet.get('adv_router')
        reason = self._validate_lsa_integrity(packet)
        if reason:
            return self._reject(reason, actor)
        if packet.get('type') == 5 and packet.get('origin') == 'asbr' and not self.allow_external:
            return self._reject('external lsa filtered', actor)
        if packet.get('type') == 3 and packet.get('origin') == 'abr' and packet.get('from_area') != self.BACKBONE:
            return self._reject('abr reinjection', actor)
        key = (packet.get('type'), packet.get('lsa_id'), actor)
        existing = self.lsdb.get(key)
        if existing is not None and packet.get('seq') <= existing.get('seq'):
            return self._reject('stale lsa', actor)
        self.lsdb[key] = dict(packet)
        return self._accept(actor)

    def on_lsr(self, packet):
        actor = packet.get('router_id')
        key = (actor, packet.get('lsa_id'))
        count = self.lsr_counts.get(key, 0)
        if count >= self.max_retransmit:
            return self._reject('retransmit limit exceeded', actor)
        self.lsr_counts[key] = count + 1
        return self._accept(actor)

    def transition(self, router_id, new_state):
        if router_id not in self.neighbors:
            self._record(False, 'unknown neighbor', router_id)
            return {'ok': False, 'reason': 'unknown neighbor'}
        current = self.neighbors[router_id]
        legal = False
        if new_state in self.STATES:
            forward = self.STATES.index(new_state) == self.STATES.index(current) + 1
            legal = new_state == current or forward or new_state in ('Down', 'Init')
        if not legal:
            self._record(False, 'illegal transition', router_id)
            return {'ok': False, 'reason': 'illegal transition'}
        self.neighbors[router_id] = new_state
        self._record(True, 'ok', router_id)
        return {'ok': True, 'state': new_state}

    @staticmethod
    def _router_id_key(router_id):
        try:
            return tuple(int(part) for part in str(router_id).split('.'))
        except ValueError:
            return (0,)

    def elect_dr(self, candidates):
        trusted = []
        for candidate in candidates:
            if candidate.get('trusted'):
                trusted.append(candidate)
            else:
                self._record(False, 'untrusted dr candidate', candidate.get('router_id'))
        trusted.sort(key=lambda c: (c.get('priority', 0), self._router_id_key(c.get('router_id'))), reverse=True)
        dr = trusted[0]['router_id'] if trusted else None
        backup = trusted[1]['router_id'] if len(trusted) > 1 else None
        return {'dr': dr, 'backup': backup}

    def lsdb_digest(self):
        canonical = []
        for key in sorted(self.lsdb, key=str):
            canonical.append([list(key), sorted(self.lsdb[key].items())])
        payload = json.dumps(canonical, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def restore_lsdb(self, lsas, expected_digest=None):
        for packet in lsas:
            reason = self._validate_lsa_integrity(packet)
            if reason:
                self._record(False, reason, packet.get('adv_router'))
                return {'restored': False, 'reason': reason}
        new_lsdb = {}
        for packet in lsas:
            key = (packet.get('type'), packet.get('lsa_id'), packet.get('adv_router'))
            new_lsdb[key] = dict(packet)
        if expected_digest is not None:
            previous = self.lsdb
            self.lsdb = new_lsdb
            digest = self.lsdb_digest()
            self.lsdb = previous
            if digest != expected_digest:
                self._record(False, 'lsdb digest mismatch')
                return {'restored': False, 'reason': 'lsdb digest mismatch'}
        self.lsdb = new_lsdb
        self._record(True, 'ok')
        return {'restored': True, 'count': len(lsas)}

    def lsdb_entry(self, lsa_type, lsa_id, adv_router):
        return self.lsdb.get((lsa_type, lsa_id, adv_router))

    def snapshot(self):
        return {
            'neighbors': dict(self.neighbors),
            'lsdb': {'|'.join(str(part) for part in key): dict(value) for key, value in self.lsdb.items()},
            'audit': list(self.audit),
        }
