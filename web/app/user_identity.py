"""Identité humaine vérifiée ; l'adresse sert au rapprochement initial seulement."""

from dataclasses import dataclass


class IdentityConflict(RuntimeError):
    """Rapprochement ambigu ou tentative de changer le propriétaire d'un compte."""


@dataclass(frozen=True)
class VerifiedKeycloakIdentity:
    issuer: str
    subject: str
    email: str

    def __post_init__(self):
        if (not isinstance(self.issuer, str) or not self.issuer
            or not isinstance(self.subject, str) or not self.subject
            or self.subject != self.subject.strip() or len(self.subject) > 255
            or any(ord(c) < 32 for c in self.subject)
            or not isinstance(self.email, str) or len(self.email) > 320
            or self.email.count('@') != 1 or self.email.startswith('@') or self.email.endswith('@')
            or any(c.isspace() or ord(c) < 32 for c in self.email)):
            raise ValueError('Identité Keycloak invalide.')
        object.__setattr__(self, 'email', self.email.lower())
