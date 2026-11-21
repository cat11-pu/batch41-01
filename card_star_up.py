"""Card star-up service.

Contract used by the tests
--------------------------
* ``MAX_STAR`` is 6.
* Raising a card from star S to S+1 consumes exactly S materials.
* A material is valid when it has the same ``template_id`` and the same
  ``quality`` as the target card.  Materials are consumed by unique instance
  id, so repeated ids in one request count once.
* Star up keeps the card ``level`` and recomputes
  ``skill_points = 2 * star + level``.
* ``preview(card_id, material_ids)`` and the following ``star_up`` for the
  same request return the same ``attack_bonus_percent`` (one shared random
  source).
* A card already at ``MAX_STAR`` cannot be upgraded.
* ``locked`` cards keep the flag after star up and cannot be decomposed.
* ``decompose`` returns the star values of the materials actually invested
  (``refund_stars``) and ``shards = 10 * sum(refund_stars)``.
* ``star_up(..., request_id=...)`` is idempotent: repeating the same
  request_id returns the first result without consuming anything or
  incrementing the task counter.
* ``task_count(task_id)`` counts distinct successful star-up requests.
* ``revert`` restores the previous star / skill points / level and puts the
  consumed materials back into the bag.
* ``transfer`` returns ``{"server", "star", "version"}``; the version starts
  at 1 and increments on every transfer.
* Mutating one card is guarded by a per-card lock.
"""


class CardStarUp:
    MAX_STAR = 6

    def __init__(self, rng=None):
        self.rng = rng

    def add_card(self, instance_id, template_id, quality, star=1, level=1, locked=False):
        raise NotImplementedError

    def consume(self, card_id, material_ids):
        raise NotImplementedError

    def preview(self, card_id, material_ids):
        raise NotImplementedError

    def star_up(self, card_id, material_ids, task_id=None, request_id=None):
        raise NotImplementedError

    def decompose(self, card_id):
        raise NotImplementedError

    def revert(self, card_id):
        raise NotImplementedError

    def transfer(self, card_id, server):
        raise NotImplementedError

    def card_state(self, instance_id):
        raise NotImplementedError

    def task_count(self, task_id):
        raise NotImplementedError
