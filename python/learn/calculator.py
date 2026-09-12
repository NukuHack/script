
print("This is a simple calculator input the things asked:");


num_1 = float(input("Enter a number: "));
operation = str(input("enter the operator (+ - / *): "))
num_2 = float(input("Enter another number: "));

result = "Something bad happened, could not process calculation";

if operation == "+":
	result=(num_1+num_2);
elif operation == "-":
	result=(num_1-num_2);
elif operation == "/":
	result=(num_1/num_2);
elif operation == "*":
	result=(num_1*num_2);
else:
	print("operator is not correct");


print(result);











input("Hit enter to close");