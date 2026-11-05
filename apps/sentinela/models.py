from django.db import models

from apps.proposicoes.models import Proposicao


class TermoSentinela(models.Model):
    """Palavra-chave da varredura temática (ex.: CBS, IBS, reforma tributária).
    Editável no Django Admin -- a coordenação adiciona/remove termos sem
    precisar de código. O sentinela busca por todos os termos ativos."""

    termo = models.CharField(max_length=120, unique=True)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Termo do sentinela'
        verbose_name_plural = 'Termos do sentinela'
        ordering = ['termo']

    def __str__(self):
        return self.termo


class ItemSentinelaVisto(models.Model):
    """Registro de proposição já vista pela varredura (por fonte + id oficial),
    para não re-alertar o mesmo item a cada execução. A primeira execução
    (--baseline) marca os atuais sem alertar."""

    FONTE_CAMARA = 'camara'
    FONTE_SENADO = 'senado'
    FONTE_CHOICES = [
        (FONTE_CAMARA, 'Câmara'),
        (FONTE_SENADO, 'Senado'),
    ]

    fonte = models.CharField(max_length=16, choices=FONTE_CHOICES)
    id_externo = models.CharField(max_length=32)
    titulo = models.CharField(max_length=255, blank=True)
    termo = models.CharField(max_length=120, blank=True)
    proposicao = models.ForeignKey(
        Proposicao,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='capturas_sentinela',
    )
    alertado = models.BooleanField(default=False)
    visto_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Item visto pelo sentinela'
        verbose_name_plural = 'Itens vistos pelo sentinela'
        ordering = ['-visto_em']
        constraints = [
            models.UniqueConstraint(fields=['fonte', 'id_externo'], name='sentinela_item_unico'),
        ]

    def __str__(self):
        return f'{self.fonte}:{self.id_externo} ({self.titulo})'
