from django.db import models


class HarnessParent(models.Model):
    value = models.IntegerField(default=0)


class HarnessChild(models.Model):
    parent = models.ForeignKey(HarnessParent, on_delete=models.CASCADE)
