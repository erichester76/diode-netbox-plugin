#!/usr/bin/env python
# Copyright 2024 NetBox Labs Inc
"""Diode NetBox Plugin - API Views."""

from typing import Any, Dict, Optional

from django.conf import settings
from packaging import version
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from extras.models import CachedValue
from rest_framework import status, views
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from utilities.api import get_serializer_for_model

from netbox_diode_plugin.api.permissions import IsDiodeReader, IsDiodeWriter
from netbox_diode_plugin.api.serializers import (
    ApplyChangeSetRequestSerializer,
    ObjectStateSerializer,
)

# Determine NetBoxType based on version
if version.parse(settings.VERSION).major >= 4:
    from core.models import ObjectType as NetBoxType
else:
    from django.contrib.contenttypes.models import ContentType as NetBoxType


def get_dynamic_queryset(model_class, object_data, lookups=None):
    """
    Limit queryset fields dynamically based on incoming data and lookups.

    Args:
        model_class: Django model class for the object type.
        object_data: Dict of incoming data fields to determine needed fields.
        lookups: Optional list of prefetch-related fields.

    Returns:
        A QuerySet with limited fields and related lookups.
    """
    requested_fields = list(object_data.keys())  # Fields from incoming data
    prefetch_fields = lookups or []

    # Combine requested fields with prefetch-related fields (flattened)
    selected_fields = set(requested_fields + [field.split("__")[0] for field in prefetch_fields])

    # Apply field selection and prefetching
    return model_class.objects.only(*selected_fields).prefetch_related(*prefetch_fields)


class ObjectStateView(views.APIView):
    """ObjectState view."""

    permission_classes = [IsAuthenticated, IsDiodeReader]

    def _get_lookups(self, object_type_model: str) -> tuple:
        """Return related object lookups, cached for performance."""
        cache_key = f"lookups_{object_type_model.lower()}"
        lookups = cache.get(cache_key)

        if not lookups:
            if "'ipam.models.ip.ipaddress'" in object_type_model:
                lookups = (
                    "assigned_object",
                    "assigned_object__device",
                    "assigned_object__device__site",
                )
            elif "'dcim.models.device_components.interface'" in object_type_model:
                lookups = "device", "device__site"
            elif "'dcim.models.devices.device'" in object_type_model:
                lookups = ("site",)
            else:
                lookups = ()
            cache.set(cache_key, lookups, timeout=3600)  # Cache for 1 hour

        return lookups

    def get(self, request, *args, **kwargs):
        """Return object state."""
        object_type = request.query_params.get("object_type")
        if not object_type:
            raise ValidationError("object_type parameter is required")

        app_label, model_name = object_type.split(".")
        cache_key = f"object_model_{app_label}_{model_name}"
        object_content_type = cache.get(cache_key)

        if not object_content_type:
            object_content_type = NetBoxType.objects.get_by_natural_key(
                app_label, model_name
            )
            cache.set(cache_key, object_content_type, timeout=3600)

        object_type_model = object_content_type.model_class()
        object_id = request.query_params.get("id")

        object_data = request.query_params.dict()  # Use request parameters as a baseline for fields

        if object_id:
            queryset = object_type_model.objects.filter(id=object_id)
        else:
            search_value = request.query_params.get("q")
            if not search_value:
                raise ValidationError("id or q parameter is required")

            query_filter = Q(value__iexact=search_value, object_type=object_content_type)
            cached_values = CachedValue.objects.filter(query_filter).values_list(
                "object_id", flat=True
            )

            lookups = self._get_lookups(str(object_type_model).lower())
            queryset = get_dynamic_queryset(
                object_type_model, object_data={"q": search_value}, lookups=lookups
            ).filter(id__in=cached_values)

        serializer = ObjectStateSerializer(
            queryset,
            many=True,
            context={"request": request, "object_type": object_type},
        )

        return Response(serializer.data[0] if serializer.data else {})


class ApplyChangeSetView(views.APIView):
    """ApplyChangeSet view."""

    permission_classes = [IsAuthenticated, IsDiodeWriter]

    @staticmethod
    def _get_object_type_model(object_type: str):
        """Cached lookup for object type model."""
        app_label, model_name = object_type.split(".")
        cache_key = f"object_model_{app_label}_{model_name}"
        model_class = cache.get(cache_key)

        if not model_class:
            object_content_type = NetBoxType.objects.get_by_natural_key(
                app_label, model_name
            )
            model_class = object_content_type.model_class()
            cache.set(cache_key, model_class, timeout=3600)

        return model_class

    def post(self, request, *args, **kwargs):
        """Apply change sets with dynamic queryset handling."""
        serializer = ApplyChangeSetRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        change_set = serializer.validated_data["change_set"]
        serializer_errors = []

        try:
            with transaction.atomic():
                for change in change_set:
                    object_type = change["object_type"]
                    change_type = change["change_type"]
                    object_data = {
                        key: value
                        for key, value in change["data"].items()
                        if value not in [None, "undefined"]
                    }
                    object_id = change.get("object_id", None)

                    model_class = self._get_object_type_model(object_type)

                    if change_type == "create":
                        instance = model_class(**object_data)
                        instance.save()
                    elif change_type == "update" and object_id:
                        instance = model_class.objects.get(id=object_id)
                        for attr, value in object_data.items():
                            setattr(instance, attr, value)
                        instance.save()
                    else:
                        serializer_errors.append(
                            {"error": f"Invalid change_type or missing object_id: {change_type}"}
                        )

                if serializer_errors:
                    raise Exception("Errors occurred during change set processing")
        except Exception as e:
            return Response({"errors": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"result": "success"}, status=status.HTTP_200_OK)
