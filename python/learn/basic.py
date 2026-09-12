# main
# helper stuff what i can not type for some reason : {}  []

def log_data(a):
	print(a, end="\n") # end is this by default but it can be changed to anything

log_data("hello world !")

def get_bigger(x):
	return x+1

print(f"{get_bigger(1)} is bigger than {1}")

# some stuff i saw in the web as a "hard thing"
def contains_num(orderedArray, num):
	leng = len(orderedArray) -1
	if orderedArray[0]>num:
		return False
	elif orderedArray[leng]<num:
		return False

	for x in range(leng):
		if orderedArray[x]==num:
			return True

	return False




gg = [0,1,2,3,5,6]

#log_data(contains_num(gg,-1))


def convert_type(x,to):
	if type(to)!=type("apple"):
		if type(x) == int:
			return float(x)
		else:
			return int(x)

apple = "10"
apple = convert_type(apple,None)
#log_data(apple)



# variable = any simple variable
# list = [] ordered and mutable
# set = {} unordered unmutable no duplicates
# tuple = () ordered unchangeable - fastest

fruits = ['apple','banana',"pineapple","bread"]
#log_data(dir(fruits)) # all of the functions
#log_data(help(fruits)) # the description of the functions
#log_data(len(fruits)) # returns the leng of the variable(s)
#log_data("milk" in fruits) # is it inside ?
#log_data(fruits[2]) # 3rd element
#log_data(fruits[0:2]) # 1;2;3 element
#log_data(fruits[:2]) # 1;2;3 element
#log_data(fruits[::2]) # every 2nd element
#log_data(fruits[::-1]) # reversed
#fruits.remove("bread")
#log_data(fruits)
#fruits.clear()
#fruits.append("orange")
#fruits.append("apple")
#fruits.insert(0,"coconut")
#log_data(fruits.index("apple"))
#log_data(fruits.count("apple"))
#fruits.sort()
#fruits.reverse()

log_data(fruits)
#for fruit in fruits:
#	log_data(fruit)




#dictionary  = {key:value} no duplicates

stuff = {
	"a" : "abcd",
	"b" : "bgtc",
	"c" : "frxw",
	"d" : "nothing",
}

#log_data(stuff.get("a"))

#if stuff.get("d"):log_data("that is a good key");
#else:log_data("bad key ....");
#stuff.update({"f":"ngbri"});
#stuff.pop("f");
#keys = stuff.keys()
#values = stuff.values()




imp = input("you alive? ")
log_data(f"Nice {imp}")






input("Hit enter to close")