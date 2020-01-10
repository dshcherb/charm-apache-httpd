build:
	rm -fr lib/ops
	cp -r ../operator/ops lib/ops
	# pip3 install --upgrade --target lib/ charmhelpers
clean:
	rm -fr lib/ops

