import time;

time_inp = int(input("enter a second countdown: "))
print(time_inp);

while True:
	time.sleep(1);
    time_inp-=1;
	if time_inp==0:
		print("Hurray: time is 0");
		break;
	else:
		print(time_inp);









input("Hit enter to close");
