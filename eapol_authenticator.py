import hashlib
import hmac
import secrets


class EapolAuthenticator:
    """802.1X port authentication with MAB fallback and guest VLAN.

    Authentication (EAPOL/RADIUS exchange), authorization (VLAN/MAC
    grant) and port state are kept strictly separate: only the
    authorization stage may move a port into ``Authenticated``, and
    every server-side failure fails closed.
    """

    STATE_UNAUTHORIZED = "Unauthorized"
    STATE_AUTHENTICATING = "Authenticating"
    STATE_AUTHENTICATED = "Authenticated"
    STATE_GUEST = "Guest"

    def __init__(self, radius_secret='radius-secret', reauth_interval=3600, mab_enabled=True, dot1x_enabled=True, guest_vlan=99, auth_vlan=10, mab_db=None):
        self.radius_secret = radius_secret
        self.reauth_interval = reauth_interval
        self.mab_enabled = mab_enabled
        self.dot1x_enabled = dot1x_enabled
        self.guest_vlan = guest_vlan
        self.auth_vlan = auth_vlan
        self.mab_db = {self.normalize_mac(mac) for mac in (mab_db or [])}
        self._ports = {}

    @staticmethod
    def normalize_mac(mac):
        digits = ''.join(c for c in mac.lower() if c in '0123456789abcdef')
        if len(digits) != 12:
            raise ValueError('invalid mac address: %r' % (mac,))
        return ':'.join(digits[i:i + 2] for i in range(0, 12, 2))

    def add_port(self, name):
        self._ports[name] = {
            'state': self.STATE_UNAUTHORIZED,
            'vlan': None,
            'authorized_macs': set(),
            'pending_mac': None,
            'challenges': {},
            'reauth_at': None,
        }

    def _port(self, name):
        if name not in self._ports:
            raise KeyError('unknown port: %r' % (name,))
        return self._ports[name]

    def port_state(self, name):
        return self._port(name)['state']

    def port_vlan(self, name):
        return self._port(name)['vlan']

    def is_authorized(self, port, mac):
        return self.normalize_mac(mac) in self._port(port)['authorized_macs']

    # ---- authorization stage (only path into Authenticated) ----

    def _authorize(self, port, mac, vlan, now, method):
        if mac is not None:
            port['authorized_macs'].add(mac)
        port['state'] = self.STATE_AUTHENTICATED
        port['vlan'] = vlan
        port['reauth_at'] = now + self.reauth_interval

    def _fail_closed(self, port):
        port['state'] = self.STATE_UNAUTHORIZED
        port['vlan'] = None
        port['pending_mac'] = None
        port['challenges'].clear()
        port['reauth_at'] = None

    # ---- EAPOL authentication stage ----

    def on_eapol(self, frame, now=0.0):
        port = self._port(frame['port'])
        frame_type = frame.get('type')
        mac = self.normalize_mac(frame['mac']) if frame.get('mac') else None
        if frame_type == 'start':
            return self._on_start(port, mac)
        if frame_type == 'identity':
            return self._on_identity(port, mac, frame.get('identity') or '')
        if frame_type == 'md5-response':
            return self._on_md5_response(port, mac, frame.get('response') or '')
        if frame_type == 'logoff':
            return self._on_logoff(port, mac)
        return {'action': 'ignore'}

    def _on_start(self, port, mac):
        if not self.dot1x_enabled:
            return {'action': 'ignore'}
        port['state'] = self.STATE_AUTHENTICATING
        port['pending_mac'] = mac
        return {'action': 'request_identity'}

    def _on_identity(self, port, mac, identity):
        if not identity.strip():
            self._fail_closed(port)
            return {'accepted': False, 'reason': 'empty identity'}
        challenge = secrets.token_hex(16)
        port['challenges'][mac] = challenge
        port['pending_mac'] = mac
        return {'accepted': True, 'challenge': challenge}

    def _on_md5_response(self, port, mac, response):
        challenge = port['challenges'].get(mac)
        expected = None
        if challenge is not None:
            seed = challenge + ':' + self.radius_secret
            expected = hashlib.md5(seed.encode('utf-8')).hexdigest()
        if expected is not None and hmac.compare_digest(expected, response.lower()):
            return {'accepted': True}
        self._fail_closed(port)
        return {'accepted': False, 'reason': 'bad response'}

    def _on_logoff(self, port, mac):
        if mac is not None:
            port['authorized_macs'].discard(mac)
            port['challenges'].pop(mac, None)
        port['state'] = self.STATE_UNAUTHORIZED
        port['vlan'] = None
        port['pending_mac'] = None
        port['reauth_at'] = None
        return {'action': 'deauthorize'}

    # ---- RADIUS authentication stage ----

    def _radius_digest(self, code):
        seed = self.radius_secret + ':' + code
        return hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]

    def on_radius(self, response):
        port = self._port(response['port'])
        if response.get('timeout'):
            self._fail_closed(port)
            return {'authorized': False, 'reason': 'radius timeout'}
        code = response.get('code') or ''
        expected = self._radius_digest(code)
        provided = str(response.get('message_authenticator') or '')
        if not hmac.compare_digest(expected, provided):
            self._fail_closed(port)
            return {'authorized': False, 'reason': 'bad message authenticator'}
        if code == 'accept':
            mac = port['pending_mac']
            self._authorize(port, mac, self.auth_vlan,
                            response.get('now', 0.0), 'dot1x')
            return {'authorized': True, 'vlan': self.auth_vlan}
        port['state'] = self.STATE_GUEST
        port['vlan'] = self.guest_vlan
        port['reauth_at'] = None
        return {'authorized': False, 'vlan': self.guest_vlan}

    # ---- MAB fallback ----

    def try_mab(self, port, mac, now=0.0):
        port_state = self._port(port)
        if not self.mab_enabled:
            return {'authorized': False, 'reason': 'mab disabled'}
        if self.dot1x_enabled and port_state['state'] == self.STATE_AUTHENTICATING:
            return {'authorized': False, 'reason': 'dot1x in progress'}
        normalized = self.normalize_mac(mac)
        if normalized not in self.mab_db:
            return {'authorized': False, 'reason': 'unknown mac'}
        self._authorize(port_state, normalized, self.auth_vlan, now, 'mab')
        return {'authorized': True, 'method': 'mab'}

    # ---- reauthentication timer ----

    def tick(self, now):
        expired = []
        for name, port in self._ports.items():
            reauth_at = port['reauth_at']
            if reauth_at is not None and now >= reauth_at:
                port['reauth_at'] = None
                port['state'] = self.STATE_AUTHENTICATING
                expired.append(name)
        return expired

    def snapshot(self):
        return {
            name: {
                'state': port['state'],
                'vlan': port['vlan'],
                'authorized_macs': sorted(port['authorized_macs']),
                'reauth_at': port['reauth_at'],
            }
            for name, port in self._ports.items()
        }
