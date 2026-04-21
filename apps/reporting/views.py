from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

class ReportingStatsView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        from apps.invoices.models import Invoice
        from django.db.models import Count, Sum
        return Response({
            "by_status": dict(
                Invoice.objects.values_list("status").annotate(c=Count("id")).values_list("status","c")
            ),
            "total_value": Invoice.objects.aggregate(t=Sum("gross_amount"))["t"] or 0,
        })
