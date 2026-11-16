import csv
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Count,Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404,redirect,render
from django.contrib.auth.decorators import user_passes_test
from django.db.models import Sum, F, ExpressionWrapper, DurationField
from datetime import timedelta, date
from .forms import RoomForm, UserEditForm
from django.utils import timezone
from django.views.decorators.http import require_POST
from rooms.models import Booking,Room

@staff_member_required
def overview(request):
 q=Booking.objects.select_related('room','user'); now=timezone.now()
 return render(request,'management/overview.html',{'rooms_total':Room.objects.count(),'active_rooms':Room.objects.filter(is_active=True).count(),'users_total':get_user_model().objects.count(),'bookings_total':q.count(),'pending_total':q.filter(status='pending').count(),'upcoming_total':q.filter(end_time__gte=now).exclude(status='cancelled').count(),'recent':q.order_by('-created_at')[:8],'popular':Room.objects.annotate(total=Count('bookings',filter=Q(bookings__status__in=['pending','confirmed']))).order_by('-total')[:5]})

@staff_member_required
def bookings(request):
 q=Booking.objects.select_related('room','user').order_by('-start_time'); s=request.GET.get('status',''); term=request.GET.get('q','').strip()[:100]
 if s in ('pending','confirmed','cancelled'): q=q.filter(status=s)
 else: s=''
 if term: q=q.filter(Q(room__name__icontains=term)|Q(user__username__icontains=term)|Q(user__email__icontains=term))
 return render(request,'management/bookings.html',{'items':q[:300],'count':q.count(),'status_filter':s,'query':term})

@staff_member_required
@require_POST
def status(request,pk):
 b=get_object_or_404(Booking.objects.select_related('room'),pk=pk); target=request.POST.get('status')
 if target not in ('confirmed','cancelled') or b.status=='cancelled' or (target=='confirmed' and b.status!='pending'):
  messages.error(request,'Недопустимое изменение статуса.'); return redirect('management:bookings')
 old=b.status; b.status=target
 try: b.save()
 except ValidationError as e:
  b.status=old; messages.error(request,'Проверка не пройдена: '+'; '.join(e.messages))
 else: messages.success(request,'Статус обновлён.')
 return redirect('management:bookings')

@staff_member_required
def rooms(request):
 q=Room.objects.annotate(total=Count('bookings',filter=Q(bookings__status__in=['pending','confirmed']))).order_by('name'); term=request.GET.get('q','').strip()[:100]
 if term: q=q.filter(Q(name__icontains=term)|Q(description__icontains=term))
 return render(request,'management/rooms.html',{'items':q,'query':term})

@staff_member_required
def users(request):
 U=get_user_model(); q=U.objects.annotate(total=Count('bookings')).order_by('username'); term=request.GET.get('q','').strip()[:100]
 if term: q=q.filter(Q(username__icontains=term)|Q(email__icontains=term))
 return render(request,'management/users.html',{'items':q[:300],'query':term})

@staff_member_required
def export_csv(request):
 q=Booking.objects.select_related('room','user').order_by('start_time'); s=request.GET.get('status','')
 if s in ('pending','confirmed','cancelled'): q=q.filter(status=s)
 response=HttpResponse(content_type='text/csv; charset=utf-8'); response['Content-Disposition']='attachment; filename="meetingroom-bookings.csv"'; response.write('\ufeff')
 w=csv.writer(response,delimiter=';'); w.writerow(['ID','Переговорная','Пользователь','Email','Начало','Окончание','Статус'])
 for b in q: w.writerow([b.pk,b.room.name,b.user.username,b.user.email,timezone.localtime(b.start_time).strftime('%d.%m.%Y %H:%M'),timezone.localtime(b.end_time).strftime('%d.%m.%Y %H:%M'),b.get_status_display()])
 return response


@staff_member_required
def room_edit(request, pk=None):
    room = get_object_or_404(Room, pk=pk) if pk is not None else None
    form = RoomForm(request.POST or None, request.FILES or None, instance=room)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(request, "Переговорная сохранена.")
        return redirect("management:rooms")
    return render(request, "management/room_form.html", {"form": form, "room": room})


@staff_member_required
@require_POST
def room_toggle(request, pk):
    room = get_object_or_404(Room, pk=pk)
    room.is_active = not room.is_active
    room.save(update_fields=["is_active"])
    messages.success(request, "Доступность комнаты изменена.")
    return redirect("management:rooms")


@staff_member_required
def user_edit(request, pk):
    target = get_object_or_404(get_user_model(), pk=pk)
    # Only a superuser can change account privileges or activation.
    if not request.user.is_superuser:
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    form = UserEditForm(request.POST or None, instance=target, allow_roles=True)
    if request.method == "POST" and form.is_valid():
        if target.pk == request.user.pk and (not form.cleaned_data["is_staff"] or not form.cleaned_data["is_active"]):
            form.add_error(None, "Нельзя отключить собственный административный доступ.")
        elif target.is_superuser and not form.cleaned_data["is_staff"]:
            form.add_error(None, "Нельзя снять права сотрудника с суперпользователя.")
        else:
            form.save()
            messages.success(request, "Пользователь обновлён.")
            return redirect("management:users")
    return render(request, "management/user_form.html", {"form": form, "target": target})


@staff_member_required
def analytics(request):
    today = timezone.localdate()
    raw_from = request.GET.get("from", "")
    raw_to = request.GET.get("to", "")
    try:
        start = date.fromisoformat(raw_from) if raw_from else today - timedelta(days=29)
        end = date.fromisoformat(raw_to) if raw_to else today
    except ValueError:
        start, end = today - timedelta(days=29), today
    if end < start or (end - start).days > 365:
        messages.error(request, "Выберите корректный период не более 366 дней.")
        start, end = today - timedelta(days=29), today
    qs = Booking.objects.filter(start_time__date__gte=start, start_time__date__lte=end).exclude(status="cancelled")
    by_room = list(Room.objects.annotate(usage=Count("bookings", filter=Q(bookings__start_time__date__gte=start, bookings__start_time__date__lte=end, bookings__status__in=["pending", "confirmed"]))).order_by("-usage", "name")[:12])
    total = qs.count()
    peak = max((r.usage for r in by_room), default=0)
    for room in by_room:
        room.percent = round(room.usage / peak * 100) if peak else 0
    return render(request, "management/analytics.html", {"start": start.isoformat(), "end": end.isoformat(), "total": total, "pending": qs.filter(status="pending").count(), "confirmed": qs.filter(status="confirmed").count(), "by_room": by_room})
